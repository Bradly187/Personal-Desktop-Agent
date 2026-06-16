"""PR 2 (R-2) — MemoryCompactor: run → local episodic note, flare-skip, kind."""

from storage.memory_compactor import MemoryCompactor


class _Res:
    def __init__(self, text, ok=True):
        self.text = text
        self.ok = ok


class _FakeRouter:
    def __init__(self, text="Fixed the parser by adding a None guard; tests passed."):
        self.text = text
        self.calls = []

    async def infer(self, domain, user_text, **kw):
        self.calls.append((domain, user_text))
        return _Res(self.text)


class _FakeMemory:
    def __init__(self, pain_day=False):
        self._pain = pain_day
        self.notes = []

    def get_pain_day_active(self):
        return self._pain

    async def write_memory_note(self, **kw):
        self.notes.append(kw)
        return len(self.notes)


class _FakeDB:
    def __init__(self, run, steps):
        self._run = run
        self._steps = steps

    async def get_agent_run(self, run_id):
        return self._run

    async def get_agent_steps(self, run_id):
        return self._steps


def _run(success=True, status="completed", goal="fix the parser", domain="code"):
    return {"id": 5, "goal": goal, "domain": domain, "success": 1 if success else 0,
            "status": status}


def _steps():
    return [
        {"step_num": 1, "action": "READ_SCREEN", "args": "", "result": "ok", "success": 1},
        {"step_num": 2, "action": "WRITE_FILE", "args": "parser.py", "result": "ok", "success": 1},
        {"step_num": 3, "action": "RUN_TERMINAL", "args": "pytest", "result": "ok", "success": 1},
    ]


async def test_summarize_success_writes_note():
    mem = _FakeMemory()
    router = _FakeRouter()
    comp = MemoryCompactor(mem, router, _FakeDB(_run(success=True), _steps()))
    nid = await comp.summarize_run(5)
    assert nid == 1
    assert len(mem.notes) == 1
    note = mem.notes[0]
    assert note["kind"] == "note"            # success → note
    assert note["goal"] == "fix the parser"
    assert note["domain"] == "code"
    assert note["source_run_id"] == 5
    assert "parser" in note["summary"].lower()
    # local model used
    assert router.calls and router.calls[0][0] == "general"


async def test_failed_run_is_recovery_kind():
    mem = _FakeMemory()
    failed_steps = _steps()
    failed_steps[-1] = {"step_num": 3, "action": "RUN_TERMINAL", "args": "pytest",
                        "result": "ERROR: 2 failed", "success": 0}
    comp = MemoryCompactor(mem, _FakeRouter(), _FakeDB(_run(success=False, status="failed"),
                                                       failed_steps))
    await comp.summarize_run(5)
    assert mem.notes[0]["kind"] == "recovery"


async def test_flare_skips_compaction():
    mem = _FakeMemory(pain_day=True)
    router = _FakeRouter()
    comp = MemoryCompactor(mem, router, _FakeDB(_run(), _steps()))
    assert await comp.summarize_run(5) is None
    assert mem.notes == []
    assert router.calls == []   # no GPU spent during a flare


async def test_missing_run_is_noop():
    mem = _FakeMemory()
    comp = MemoryCompactor(mem, _FakeRouter(), _FakeDB(None, []))
    assert await comp.summarize_run(99) is None
    assert mem.notes == []


async def test_empty_summary_writes_nothing():
    mem = _FakeMemory()
    comp = MemoryCompactor(mem, _FakeRouter(text="  "), _FakeDB(_run(), _steps()))
    assert await comp.summarize_run(5) is None
    assert mem.notes == []

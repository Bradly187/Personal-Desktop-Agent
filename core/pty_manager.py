"""PTY Session Manager.

Manages background pseudoterminal sessions (using subprocess and pipes) 
for interactive terminal applications.
"""

import subprocess
import threading
import time
import uuid
import logging
from collections import deque
from typing import Dict, Optional


log = logging.getLogger(__name__)

class PTYSession:
    def __init__(self, command: str, session_id: str):
        self.command = command
        self.session_id = session_id
        self.process: Optional[subprocess.Popen] = None
        self.buffer = deque(maxlen=1000)
        self.lock = threading.Lock()
        self.reader_thread: Optional[threading.Thread] = None
        self.last_activity = time.time()
        self._start_process()

    def _start_process(self):
        # We will wrap in wsl.exe if we need WSL routing, just like sandbox does.
        # But for simplicity, we just use standard subprocess here.
        # This will be enhanced later to use inference.sandbox rules.
        
        # Determine command wrapper (simplified sandbox logic for PTY)
        cmd_args = self.command
        # On Windows, we just run the command in shell
        
        try:
            self.process = subprocess.Popen(
                cmd_args,
                shell=True,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1, # Line buffered
                encoding='utf-8',
                errors='replace'
            )
            self.reader_thread = threading.Thread(target=self._read_loop, daemon=True)
            self.reader_thread.start()
        except Exception as e:
            log.error(f"Failed to spawn PTY {self.session_id}: {e}")
            with self.lock:
                self.buffer.append(f"Error starting process: {e}\n")

    def _read_loop(self):
        if not self.process or not self.process.stdout:
            return
            
        while True:
            # Read 1 character at a time to remain responsive for interactive prompts
            char = self.process.stdout.read(1)
            if not char:
                break
            with self.lock:
                self.buffer.append(char)
                self.last_activity = time.time()
                
    def read_stream(self, max_lines: int = 100) -> str:
        with self.lock:
            self.last_activity = time.time()
            buffer: bytes = b"".join(self.buffer)
            output = "".join(self.buffer)
            self.buffer.clear()
            
            # Split and truncate if necessary
            lines = output.split('\n')
            if len(lines) > max_lines:
                lines = lines[-max_lines:]
            return '\n'.join(lines)
            
    def send_input(self, text: str):
        if self.process and self.process.stdin and self.process.poll() is None:
            with self.lock:
                self.last_activity = time.time()
            try:
                self.process.stdin.write(text)
                self.process.stdin.flush()
            except OSError as e:
                log.error(f"Failed to send input to {self.session_id}: {e}")

    def is_alive(self) -> bool:
        return self.process is not None and self.process.poll() is None
        
    def terminate(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()

class PTYManager:
    def __init__(self):
        self.sessions: Dict[str, PTYSession] = {}
        self.lock = threading.Lock()
        
    def spawn(self, command: str) -> str:
        session_id = f"pty_{uuid.uuid4().hex[:8]}"
        session = PTYSession(command, session_id)
        with self.lock:
            self.sessions[session_id] = session
        return session_id
        
    def get_session(self, session_id: str) -> Optional[PTYSession]:
        with self.lock:
            return self.sessions.get(session_id)
            
    def terminate(self, session_id: str):
        with self.lock:
            if session_id in self.sessions:
                self.sessions[session_id].terminate()
                del self.sessions[session_id]

# Singleton instance
manager = PTYManager()

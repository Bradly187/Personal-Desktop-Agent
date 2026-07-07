"""Retry utility — fixture for dev_critic eval (dc-06)."""



def with_timeout(fn, timeout):
    """Call fn(); raise TimeoutError if it takes longer than timeout seconds."""
    import threading
    result = [None]
    exc = [None]

    def target():
        try:
            result[0] = fn()
        except Exception as e:
            exc[0] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"fn() did not complete in {timeout}s")
    if exc[0] is not None:
        raise exc[0]
    return result[0]

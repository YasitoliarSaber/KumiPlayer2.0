"""进程内持久化读改写与快照维护锁。"""

import threading

DATA_WRITE_LOCK = threading.RLock()

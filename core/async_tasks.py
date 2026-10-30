# ============================================================
# 异步任务处理器 - 文档入库异步化 + 队列管理
# ============================================================
import asyncio
import threading
import queue
import time
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Any, Optional

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AsyncTask:
    """异步任务定义"""
    task_id: str
    name: str
    status: TaskStatus = TaskStatus.PENDING
    result: Any = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "name": self.name,
            "status": self.status.value,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
        }


class AsyncTaskProcessor:
    """异步任务处理器"""
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._tasks = {}
                    cls._instance._queue = queue.Queue()
                    cls._instance._worker = None
                    cls._instance._running = False
        return cls._instance

    def start(self):
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._process_queue, daemon=True)
        self._worker.start()
        logger.info("AsyncTaskProcessor worker started")

    def stop(self):
        self._running = False
        logger.info("AsyncTaskProcessor stopped")

    def submit(self, task_name: str, func: Callable, *args, **kwargs) -> str:
        """提交异步任务，返回任务ID"""
        import uuid
        task_id = str(uuid.uuid4())[:8]
        task = AsyncTask(task_id=task_id, name=task_name)
        self._tasks[task_id] = task
        self._queue.put((task_id, func, args, kwargs))
        self.start()
        return task_id

    def _process_queue(self):
        while self._running:
            try:
                task_id, func, args, kwargs = self._queue.get(timeout=1)
                task = self._tasks.get(task_id)
                if task:
                    task.status = TaskStatus.PROCESSING
                    try:
                        task.result = func(*args, **kwargs)
                        task.status = TaskStatus.COMPLETED
                    except Exception as e:
                        task.status = TaskStatus.FAILED
                        task.error = str(e)
                        logger.error(f"Task {task_id} failed: {e}")
                    task.completed_at = time.time()
                self._queue.task_done()
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Task processor error: {e}")

    def get_task(self, task_id: str) -> Optional[dict]:
        """获取任务状态"""
        task = self._tasks.get(task_id)
        return task.to_dict() if task else None

    def get_pending_count(self) -> int:
        return self._queue.qsize()

    def clean_old_tasks(self, max_age: float = 3600):
        """清理过期任务（默认1小时）"""
        now = time.time()
        to_delete = []
        for tid, task in self._tasks.items():
            if task.completed_at and (now - task.completed_at) > max_age:
                to_delete.append(tid)
        for tid in to_delete:
            del self._tasks[tid]


# 全局单例
async_processor = AsyncTaskProcessor()

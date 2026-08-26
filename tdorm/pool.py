"""轻量连接复用池：按需建连、池满阻塞等待、坏连接可丢弃（容量自愈）。

线程安全：``acquire`` / ``release`` / ``discard`` / ``close`` 均可多线程调用。
"""

from __future__ import annotations

import queue
import threading

from .exceptions import PoolTimeoutError

__all__ = ["ConnectionPool"]


class ConnectionPool:
    def __init__(self, factory, size: int, timeout: float = 5.0):
        """``factory``：无参返回一个连接的函数（如 engine._connect）。"""
        if not isinstance(size, int) or size < 1:
            raise ValueError("连接池大小必须为正整数，得到 %r" % (size,))
        if timeout and timeout <= 0:
            raise ValueError("连接池等待超时必须为正数，得到 %r" % (timeout,))
        self._factory = factory
        self._size = size
        self._timeout = timeout
        self._queue = queue.Queue(maxsize=size)
        self._created = 0  # 已创建(未销毁)的连接数
        self._closed = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 状态
    @property
    def size(self) -> int:
        return self._size

    @property
    def created(self) -> int:
        return self._created

    @property
    def idle(self) -> int:
        return self._queue.qsize()

    # ------------------------------------------------------------ 核心
    def acquire(self):
        """取一个连接；池空则新建（未达上限），否则阻塞等待归还。"""
        if self._closed:
            raise PoolTimeoutError("连接池已关闭")
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            pass
        with self._lock:
            if not self._closed and self._created < self._size:
                self._created += 1
                conn = self._factory()
                return conn
        try:
            return self._queue.get(timeout=self._timeout)
        except queue.Empty:
            raise PoolTimeoutError(
                "等待连接池归还超时（%.1fs），池大小 %d，已创建 %d"
                % (self._timeout, self._size, self._created)
            )

    def release(self, conn):
        """归还连接；池满或已关闭时直接关闭。"""
        if conn is None:
            return
        if self._closed:
            self._close(conn)
            return
        try:
            self._queue.put_nowait(conn)
        except queue.Full:
            self._close(conn)

    def discard(self, conn):
        """丢弃坏连接：关闭并让出容量（下次 acquire 会补建）。"""
        if conn is None:
            return
        self._close(conn)
        with self._lock:
            self._created = max(0, self._created - 1)

    def close(self):
        """关闭池内所有空闲连接并禁止再取。"""
        self._closed = True
        while True:
            try:
                self._close(self._queue.get_nowait())
            except queue.Empty:
                break

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _close(conn):
        try:
            conn.close()
        except Exception:  # pragma: no cover
            pass
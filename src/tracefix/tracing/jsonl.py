"""JSON Lines 轨迹持久化实现。"""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from types import TracebackType

from tracefix.exceptions import TraceProtocolError
from tracefix.tracing.base import TraceEvent


class JSONLTraceSink:
    """把每条轨迹事件写成一行 UTF-8 JSON，并在每次写入后刷新。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._stream = self.path.open("w", encoding="utf-8", newline="\n")
        except OSError as exc:
            raise TraceProtocolError(
                f"cannot open JSONL trace file: {exc}",
                context={"path": str(self.path)},
            ) from exc
        self._lock = Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        """返回接收器是否已经关闭。"""
        return self._closed

    def write(self, event: TraceEvent) -> None:
        """写入一条完整事件；失败时转换为稳定轨迹异常。"""
        with self._lock:
            if self._closed:
                raise TraceProtocolError(
                    "cannot write to a closed JSONL trace sink",
                    context={"path": str(self.path)},
                )
            try:
                self._stream.write(event.model_dump_json() + "\n")
                # 实时刷新能在进程异常退出时尽量保住已经完成的轨迹。
                self._stream.flush()
            except OSError as exc:
                raise TraceProtocolError(
                    f"cannot write JSONL trace event: {exc}",
                    context={"path": str(self.path), "event_id": event.id},
                ) from exc

    def close(self) -> None:
        """幂等关闭文件；外部传入 Agent 的 sink 仍由创建者负责关闭。"""
        with self._lock:
            if self._closed:
                return
            try:
                self._stream.close()
            except OSError as exc:
                raise TraceProtocolError(
                    f"cannot close JSONL trace file: {exc}",
                    context={"path": str(self.path)},
                ) from exc
            finally:
                self._closed = True

    def __enter__(self) -> JSONLTraceSink:
        """返回当前接收器，便于 Runner 使用 with 管理生命周期。"""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """离开上下文时关闭轨迹文件。"""
        self.close()

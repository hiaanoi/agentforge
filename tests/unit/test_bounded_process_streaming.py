import hashlib
from io import BytesIO

from agentforge.process.streaming import capture_stream


class GuardedStream(BytesIO):
    def __init__(self, value: bytes, maximum_read: int) -> None:
        super().__init__(value)
        self.maximum_read = maximum_read
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        assert 0 < size <= self.maximum_read
        self.read_sizes.append(size)
        return super().read(size)


def test_capture_stream_bounds_retained_bytes_but_hashes_complete_stream() -> None:
    content = b"0123456789" * 100
    stream = GuardedStream(content, maximum_read=17)

    captured = capture_stream(stream, max_output_bytes=23, chunk_size=17)

    assert stream.read_sizes
    assert captured.size == len(content)
    assert captured.sha256_digest == hashlib.sha256(content).hexdigest()
    assert len(captured.retained_bytes) == 23
    assert captured.retained_bytes == content[:23]
    assert captured.truncated is True


def test_capture_stream_normalizes_invalid_text_and_control_characters() -> None:
    captured = capture_stream(
        BytesIO(b"hello\xff\x00\x01\nworld\t!"),
        max_output_bytes=100,
        chunk_size=4,
    )

    assert "\ufffd" in captured.summary
    assert "\x00" not in captured.summary
    assert "\x01" not in captured.summary
    assert "\n" in captured.summary
    assert "\t" in captured.summary


def test_capture_stream_redacts_credentials_before_result_construction() -> None:
    secret = "sk-" + "a" * 40
    private_key = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----"
    content = f"OPENAI_API_KEY={secret}\n{private_key}\n".encode()

    captured = capture_stream(
        BytesIO(content),
        max_output_bytes=4096,
        chunk_size=16,
    )

    assert secret not in captured.summary
    assert "BEGIN PRIVATE KEY" not in captured.summary
    assert "<redacted>" in captured.summary
    assert captured.sha256_digest == hashlib.sha256(content).hexdigest()

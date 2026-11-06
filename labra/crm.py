from typing import Protocol


class RateLimited(Exception):
    def __init__(self, object_name: str, retry_after_ms: int):
        super().__init__(f"{object_name} rate limited, retry after {retry_after_ms}ms")
        self.object_name = object_name
        self.retry_after_ms = retry_after_ms


class CrmRecordNotFound(Exception):
    def __init__(self, object_name: str, record_id: str):
        super().__init__(f"{object_name} {record_id} not found")
        self.object_name = object_name
        self.record_id = record_id


class CrmClient(Protocol):
    def fetch(self, object_name: str, record_id: str, fields: list[str]) -> dict: ...


class FakeCrm:
    def __init__(self):
        self._records = {}
        self.calls = []
        self._fail_once = set()

    def seed(self, object_name, record_id, values):
        self._records[(object_name, record_id)] = values

    def fail_next(self, object_name):
        """Next fetch of this object raises RateLimited. The one after succeeds."""
        self._fail_once.add(object_name)

    def fetch(self, object_name, record_id, fields):
        self.calls.append({"object": object_name, "fields": list(fields)})
        if object_name in self._fail_once:
            self._fail_once.remove(object_name)
            raise RateLimited(object_name, retry_after_ms=5)
        row = self._records.get((object_name, record_id))
        if row is None:
            raise CrmRecordNotFound(object_name, record_id)
        return {field: row[field] for field in fields if field in row}

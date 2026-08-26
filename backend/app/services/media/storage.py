"""
Storage abstraction layer.
Local filesystem now, S3 later. Same interface either way.
"""
import os
import shutil
from pathlib import Path
from typing import Optional
from abc import ABC, abstractmethod


class StorageBackend(ABC):
    """Abstract storage interface - swap local for S3 without changing callers."""

    @abstractmethod
    def read_bytes(self, path: str) -> bytes:
        pass

    @abstractmethod
    def write_bytes(self, path: str, data: bytes) -> str:
        pass

    @abstractmethod
    def write_text(self, path: str, text: str) -> str:
        pass

    @abstractmethod
    def read_text(self, path: str) -> str:
        pass

    @abstractmethod
    def exists(self, path: str) -> bool:
        pass

    @abstractmethod
    def get_temp_path(self, path: str) -> str:
        """Get a local file path for processing. For S3, this downloads to temp."""
        pass

    @abstractmethod
    def get_url(self, path: str) -> str:
        """Get a URL or path for the file. S3 returns presigned URL."""
        pass


class LocalStorage(StorageBackend):
    """Local filesystem storage. Files accessed directly by path."""

    def __init__(self, base_dir: Optional[str] = None):
        self.base_dir = base_dir

    def _resolve(self, path: str) -> str:
        if self.base_dir and not os.path.isabs(path):
            return os.path.join(self.base_dir, path)
        return path

    def read_bytes(self, path: str) -> bytes:
        with open(self._resolve(path), "rb") as f:
            return f.read()

    def write_bytes(self, path: str, data: bytes) -> str:
        full_path = self._resolve(path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(data)
        return full_path

    def write_text(self, path: str, text: str) -> str:
        full_path = self._resolve(path)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(text)
        return full_path

    def read_text(self, path: str) -> str:
        with open(self._resolve(path), "r", encoding="utf-8") as f:
            return f.read()

    def exists(self, path: str) -> bool:
        return os.path.exists(self._resolve(path))

    def get_temp_path(self, path: str) -> str:
        """Local storage: file is already local, just return the path."""
        return self._resolve(path)

    def get_url(self, path: str) -> str:
        return f"file:///{self._resolve(path).replace(os.sep, '/')}"


class S3Storage(StorageBackend):
    """
    S3 storage backend. Placeholder for AWS migration.
    Will use boto3 s3 client with presigned URLs for Transcribe/Bedrock access.
    """

    def __init__(self, bucket: str, prefix: str = "", region: str = "us-east-1"):
        self.bucket = bucket
        self.prefix = prefix
        self.region = region
        # self.client = boto3.client("s3", region_name=region)

    def _key(self, path: str) -> str:
        if self.prefix:
            return f"{self.prefix}/{path}"
        return path

    def read_bytes(self, path: str) -> bytes:
        raise NotImplementedError("S3Storage not yet active - configure bucket first")

    def write_bytes(self, path: str, data: bytes) -> str:
        raise NotImplementedError("S3Storage not yet active")

    def write_text(self, path: str, text: str) -> str:
        raise NotImplementedError("S3Storage not yet active")

    def read_text(self, path: str) -> str:
        raise NotImplementedError("S3Storage not yet active")

    def exists(self, path: str) -> bool:
        raise NotImplementedError("S3Storage not yet active")

    def get_temp_path(self, path: str) -> str:
        raise NotImplementedError("S3Storage not yet active")

    def get_url(self, path: str) -> str:
        return f"s3://{self.bucket}/{self._key(path)}"


# Global storage instance - swap this to S3Storage when ready
storage = LocalStorage()

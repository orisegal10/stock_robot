import yaml
from pathlib import Path
from loguru import logger


class Config:
    def __init__(self, path: str = "config.yaml"):
        self._path = Path(path)
        self._data = self._load()
        logger.info("Configuration loaded from {}", self._path)

    def _load(self) -> dict:
        if not self._path.exists():
            raise FileNotFoundError(f"config.yaml not found at {self._path.resolve()}")
        with open(self._path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def get(self, *keys, default=None):
        """Nested key access: config.get('risk', 'max_open_positions')"""
        node = self._data
        for key in keys:
            if not isinstance(node, dict):
                return default
            node = node.get(key)
            if node is None:
                return default
        return node

    def __getitem__(self, key):
        return self._data[key]


# Module-level singleton — import this everywhere
config = Config()

"""Base class for format readers/writers"""

from abc import ABC, abstractmethod
import pandas as pd


class Format(ABC):
    @abstractmethod
    def read(self, content: str) -> pd.DataFrame:
        """Parse string content to DataFrame."""
        pass

    @abstractmethod
    def write(self, df: pd.DataFrame) -> str:
        """Serialize DataFrame to string."""
        pass

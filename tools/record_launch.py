"""Notify the manager of an exec-preserving launcher start using its existing record."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from mlx_activation_lab.manager_models import remember_start

if __name__ == "__main__":
    remember_start(int(sys.argv[1]), sys.argv[2], thinking=sys.argv[3] != "off")

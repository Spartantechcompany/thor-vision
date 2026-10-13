import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault("SCENES_PATH", os.path.join(os.path.dirname(os.path.dirname(__file__)), "config", "scenes.yml"))

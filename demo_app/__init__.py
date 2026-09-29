"""Local project-management demo used only by the Agent testing system."""

from demo_app.app import DEMO_VERSION, DemoAppServer
from demo_app.bugs import GroundTruthReader, SeededBugs

__all__ = ["DEMO_VERSION", "DemoAppServer", "GroundTruthReader", "SeededBugs"]

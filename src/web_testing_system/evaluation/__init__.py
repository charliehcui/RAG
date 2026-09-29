"""Metrics, evaluation modes, and the explicit Full Evaluation gate."""

from web_testing_system.evaluation.metrics import (
    GroundTruthComparison,
    MetricsCalculator,
)
from web_testing_system.evaluation.runner import (
    EvaluationControls,
    EvaluationExecution,
    EvaluationMode,
    EvaluationPlan,
    EvaluationRunner,
    EvaluationRunRecord,
    FinalAcceptanceChecker,
    FinalAcceptanceResult,
    FullEvaluationGate,
    FullEvaluationGateResult,
    build_evaluation_plan,
    build_execution_route,
    summarize_evaluation_runs,
)

__all__ = [
    "EvaluationControls",
    "EvaluationExecution",
    "EvaluationMode",
    "EvaluationPlan",
    "EvaluationRunRecord",
    "EvaluationRunner",
    "FinalAcceptanceChecker",
    "FinalAcceptanceResult",
    "FullEvaluationGate",
    "FullEvaluationGateResult",
    "GroundTruthComparison",
    "MetricsCalculator",
    "build_evaluation_plan",
    "build_execution_route",
    "summarize_evaluation_runs",
]

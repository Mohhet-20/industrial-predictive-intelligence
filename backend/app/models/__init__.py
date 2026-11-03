"""Import every model so app.db.base.Base.metadata is complete for Alembic
autogenerate and for create_all() in tests."""
from app.models.organization import Organization  # noqa: F401
from app.models.user import User, UserRole  # noqa: F401
from app.models.asset import Asset  # noqa: F401
from app.models.sensor import Sensor  # noqa: F401
from app.models.data_source import DataSource, DataSourceType  # noqa: F401
from app.models.ingestion import IngestionBatch, IngestionStatus  # noqa: F401
from app.models.measurement import SensorMeasurement  # noqa: F401
from app.models.data_quality import (  # noqa: F401
    DataQualityIssue,
    DataQualityIssueType,
    DataQualityIssueSeverity,
)
from app.models.job import BackgroundJob, JobStatus, JobType  # noqa: F401
from app.models.feature import FeatureDefinition, DatasetBuild  # noqa: F401
from app.models.intelligence import (  # noqa: F401
    DetectorType,
    ModelVersion,
    ModelVersionStatus,
    ModelRun,
    ModelRunStatus,
    Anomaly,
    AnomalyStatus,
    AnomalySeverityTier,
    AnomalyInvestigation,
    MaintenanceEvent,
)
from app.models.condition import (  # noqa: F401
    ConditionRun,
    ConditionRunStatus,
    DegradationSignal,
    DegradationStage,
    ConditionSignalStatus,
    DegradationReview,
    ForecastMethod,
    ForecastModel,
    ForecastModelStatus,
    ForecastRun,
    ForecastRunStatus,
)
from app.models.mlops import (  # noqa: F401
    ComponentType,
    VersionKind,
    LifecycleStage,
    IntelligenceComponent,
    ComponentVersion,
    GovernanceEventType,
    GovernanceEvent,
    ShiftAnalysisRun,
    RetrainingRequestStatus,
    RetrainingRequest,
)
from app.models.investigation import (  # noqa: F401
    InvestigationStatus,
    InvestigationEvidenceType,
    InvestigationAnnotationType,
    Investigation,
    InvestigationEvidenceLink,
    InvestigationStatusEvent,
    InvestigationAnnotation,
    InvestigationRelation,
)
from app.models.copilot import (  # noqa: F401
    CopilotContextType,
    CopilotConversation,
    CopilotMessageRole,
    CopilotMessage,
)
from app.models.scenario import (  # noqa: F401
    ScenarioType,
    ScenarioStatus,
    Scenario,
    ScenarioRunStatus,
    ScenarioRunKind,
    ScenarioRun,
)
from app.models.outcome import (  # noqa: F401
    OutcomeType,
    OutcomeSource,
    VerificationStatus,
    Outcome,
    OutcomeVerification,
    OutcomeSignalType,
    SignalAssessment,
    OutcomeSignalLink,
    EvaluationKind,
    OutcomeEvaluationRun,
    RetrainingCandidateStatus,
    RetrainingCandidate,
    FeedbackDatasetStatus,
    FeedbackDatasetBuild,
    PrioritizationPolicyStatus,
    PrioritizationPolicy,
)

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface ApiErrorBody {
  error: {
    code: string;
    message: string;
    request_id?: string | null;
    details?: unknown;
  };
}

export type UserRole = "admin" | "engineer" | "viewer";

export interface UserResponse {
  id: string;
  email: string;
  full_name: string;
  role: UserRole;
  organization_id: string;
  organization_name: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in_minutes: number;
}

export interface AssetResponse {
  id: string;
  code: string;
  name: string;
  asset_type: string;
  location: string | null;
  description: string | null;
  sensor_count: number;
}

export interface SensorResponse {
  id: string;
  asset_id: string;
  code: string;
  name: string;
  measurement_type: string;
  unit: string;
  expected_min: number | null;
  expected_max: number | null;
  expected_interval_seconds: number | null;
}

export interface MeasurementResponse {
  id: string;
  asset_id: string;
  sensor_id: string;
  timestamp: string;
  value: number;
  ingestion_batch_id: string | null;
}

export interface SensorSummaryStats {
  sensor_id: string;
  count: number;
  min_value: number | null;
  max_value: number | null;
  mean_value: number | null;
  std_dev: number | null;
  first_timestamp: string | null;
  last_timestamp: string | null;
}

export type DataQualityIssueType =
  | "missing_value"
  | "out_of_range"
  | "duplicate_observation"
  | "timestamp_out_of_order"
  | "timestamp_future"
  | "data_gap"
  | "unsupported_unit"
  | "malformed_record";

export type DataQualityIssueSeverity = "info" | "warning" | "critical";

export interface DataQualityIssueResponse {
  id: string;
  asset_id: string;
  sensor_id: string | null;
  issue_type: DataQualityIssueType;
  severity: DataQualityIssueSeverity;
  period_start: string;
  period_end: string;
  description: string;
  details: Record<string, unknown> | null;
  detected_at: string;
}

export interface DataQualitySummary {
  asset_id: string;
  total_issues: number;
  critical_count: number;
  warning_count: number;
  info_count: number;
  issues_by_type: Record<string, number>;
}

export type IngestionStatus = "pending" | "running" | "succeeded" | "succeeded_with_errors" | "failed";

export interface IngestionBatchResponse {
  id: string;
  status: IngestionStatus;
  data_source_id: string;
  records_submitted: number;
  records_accepted: number;
  records_rejected: number;
  records_duplicate: number;
  created_at: string;
  completed_at: string | null;
}

export interface ReadinessResponse {
  status: string;
  environment: string;
  schema_version: string | null;
  checks: Record<string, string>;
}

// ---------------------------------------------------------------------------
// Reliability & observability (Phase 7)
// ---------------------------------------------------------------------------

export interface SystemMetricsResponse {
  window: { lookback_days: number; as_of: string };
  request_metrics: {
    total_requests_recorded: number;
    requests_by_status_class: Record<string, number>;
    recent_sample_size: number;
    recent_sample_capacity: number;
    latency_ms: { avg: number | null; p50: number | null; p95: number | null; max: number | null };
    note: string;
  };
  process: { uptime_seconds: number };
  background_jobs: {
    by_type_and_status: Record<string, Record<string, number>>;
    avg_duration_seconds_succeeded: number | null;
  };
  ingestion: { batches_by_status: Record<string, number> };
  data_quality: { issues_detected_by_severity: Record<string, number> };
  anomaly_detection: {
    runs_by_status: Record<string, number>;
    failed_runs: number;
    anomalies_by_severity: Record<string, number>;
  };
  note: string;
}

// ---------------------------------------------------------------------------
// Time-Series Intelligence (Phase 2)
// ---------------------------------------------------------------------------

export interface AnalysisEnvelope<T> {
  data: T;
}

export interface SensorProfile {
  requested_range: { start: string; end: string };
  unit: string;
  observation_count: number;
  insufficient_data: boolean;
  insufficient_data_reason: string | null;
  observed_range: { first_timestamp: string | null; last_timestamp: string | null } | null;
  sampling_interval: Record<string, number | null> | null;
  missingness: { expected_count: number; missing_count: number; missing_fraction: number | null } | null;
  gaps: Array<{ start: string; end: string; duration_seconds: number }>;
  gap_count: number;
  descriptive_stats: {
    min: number;
    max: number;
    mean: number;
    median: number;
    std: number;
    variance: number;
    quantiles: Record<string, number>;
  } | null;
  distribution: { skewness: number | null; kurtosis: number | null; note: string | null } | null;
  trend: {
    method: string;
    slope_per_hour: number | null;
    r_squared: number | null;
    p_value: number | null;
    direction: string | null;
    strength: string | null;
    note: string | null;
  } | null;
  rolling_behavior: {
    window_observation_count: number;
    points: Array<{ timestamp: string; rolling_mean: number | null; rolling_std: number | null }>;
  } | null;
  change_rate: { mean_absolute_step_change: number; mean_absolute_change_per_hour: number | null } | null;
  data_quality_warnings: string[];
}

export interface ChangeCandidate {
  type: "level_shift" | "variance_shift";
  timestamp: string;
  score: number;
  strength: "weak" | "moderate" | "strong";
  description: string;
  window_observation_count: number;
  mean_before?: number;
  mean_after?: number;
  variance_before?: number;
  variance_after?: number;
  variance_ratio?: number;
}

export interface ChangeCandidatesResult {
  sensor_id: string;
  candidates: ChangeCandidate[];
  methodology: string;
  window_observation_count: number;
  note: string;
}

export type FeatureKind =
  | "mean"
  | "median"
  | "std"
  | "variance"
  | "min"
  | "max"
  | "quantile"
  | "iqr"
  | "lag"
  | "rolling_mean"
  | "rolling_std"
  | "rate_of_change"
  | "diff"
  | "ema"
  | "skewness"
  | "kurtosis"
  | "spectral_dominant_period"
  | "spectral_energy";

export const WINDOWED_FEATURE_KINDS: FeatureKind[] = [
  "mean",
  "median",
  "std",
  "variance",
  "min",
  "max",
  "quantile",
  "iqr",
  "rolling_mean",
  "rolling_std",
  "rate_of_change",
  "ema",
  "skewness",
  "kurtosis",
  "spectral_dominant_period",
  "spectral_energy",
];

export interface FeatureResult {
  kind: string;
  category: string | null;
  value: number | null;
  computed: boolean;
  reason: string | null;
  observation_count_used: number;
  window_start: string | null;
  window_end: string | null;
}

export interface FeaturePreviewResultItem {
  feature_name: string;
  result: FeatureResult;
}

export interface FeaturePreviewResponse {
  as_of: string;
  results: FeaturePreviewResultItem[];
}

export interface CorrelationPair {
  sensor_a: string;
  sensor_b: string;
  correlation: number | null;
  overlapping_observations: number;
  note: string | null;
}

export interface CorrelationResult {
  method: string;
  pairs: CorrelationPair[];
  caveat: string;
}

// ---------------------------------------------------------------------------
// Anomaly Detection Intelligence (Phase 3)
// ---------------------------------------------------------------------------

export type DetectorType = "robust_zscore" | "isolation_forest" | "mahalanobis";
export type ModelVersionStatus = "trained" | "failed" | "archived";
export type ModelRunStatus = "queued" | "running" | "succeeded" | "failed";
export type AnomalyStatus = "detected" | "under_review" | "acknowledged" | "resolved" | "false_positive";
export type AnomalySeverityTier = "observed_unusual_behavior" | "candidate_anomaly" | "high_priority_anomaly";

export interface DetectorResponse {
  id: string;
  organization_id: string;
  name: string;
  version: number;
  detector_type: DetectorType;
  detector_config: Record<string, unknown>;
  reference_period_start: string;
  reference_period_end: string;
  reference_observation_count: number;
  feature_schema: { feature_names: string[] } | null;
  baseline_summary: Record<string, unknown> | null;
  status: ModelVersionStatus;
  training_error: string | null;
  trained_at: string | null;
  evaluation_results: {
    is_synthetic: boolean;
    detector_type: string;
    scenario_count: number;
    scenario_count_evaluated: number;
    scenario_count_skipped: number;
    mean_precision: number | null;
    mean_recall: number | null;
    interpretation_note: string;
    evaluated_at: string;
    scenarios: Array<{
      scenario: string;
      is_synthetic: boolean;
      description?: string;
      metrics: { precision: number | null; recall: number | null; f1: number | null; true_positives: number; false_positives: number; false_negatives: number } | null;
      skipped_reason?: string;
    }>;
  } | null;
  created_at: string;
  created_by_user_id: string | null;
}

export interface DetectionRunResponse {
  id: string;
  organization_id: string;
  model_version_id: string;
  status: ModelRunStatus;
  scored_period_start: string;
  scored_period_end: string;
  background_job_id: string | null;
  error_message: string | null;
  metrics: { points_scored: number; points_flagged: number; anomalies_created: number; note: string | null; retrospective: boolean } | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  created_by_user_id: string | null;
}

export interface AnomalyResponse {
  id: string;
  organization_id: string;
  asset_id: string;
  sensor_id: string | null;
  model_run_id: string;
  status: AnomalyStatus;
  period_start: string;
  period_end: string;
  summary: string;
  detector_type: DetectorType;
  raw_score: number;
  normalized_score: number | null;
  threshold_used: number;
  severity_tier: AnomalySeverityTier;
  contributing_signals: Record<string, unknown> | null;
  data_quality_context: { observation_count: number; missing_fraction: number | null; gap_count: number; warnings: string[] } | null;
  is_synthetic_scenario: boolean;
  created_at: string;
  updated_at: string;
}

export interface AnomalyContext {
  anomaly_id: string;
  sensor_id?: string;
  note?: string;
  context_window?: { start: string; end: string };
  before: { observation_count: number; mean: number; min: number; max: number } | null;
  during: { observation_count: number; mean: number; min: number; max: number } | null;
  after: { observation_count: number; mean: number; min: number; max: number } | null;
  series?: Array<{ timestamp: string; value: number }>;
}

export interface AnomalyInvestigationResponse {
  id: string;
  anomaly_id: string;
  reviewed_by_user_id: string | null;
  from_status: AnomalyStatus;
  to_status: AnomalyStatus;
  notes: string | null;
  created_at: string;
}

export interface AnomalyMonitoringSummary {
  detector_count_by_status: Record<string, number>;
  run_count_by_status: Record<string, number>;
  detector_usage: Array<{ model_version_id: string; name: string; version: number; detector_type: string; run_count: number }>;
  anomaly_count_by_severity_tier: Record<string, number>;
  anomaly_count_by_status: Record<string, number>;
  total_points_scored_succeeded_runs: number;
  recent_failed_runs: Array<{ run_id: string; model_version_id: string; error_message: string | null; finished_at: string | null }>;
  not_implemented: string[];
}

export type JobStatus = "queued" | "running" | "succeeded" | "failed";

// ---------------------------------------------------------------------------
// Equipment Condition Intelligence & Forecasting (Phase 4)
// ---------------------------------------------------------------------------

export type DegradationStage = "observed_trend" | "degradation_candidate" | "validated_degradation";
export type ConditionSignalStatus = "detected" | "under_review" | "acknowledged" | "resolved" | "false_positive";
export type ConditionRunStatus = "succeeded" | "failed";
export type ForecastModelStatus = "justified" | "not_justified" | "failed";
export type ForecastRunStatus = "succeeded" | "failed";

export interface ConditionRunResponse {
  id: string;
  organization_id: string;
  asset_id: string;
  sensor_id: string;
  analysis_period_start: string;
  analysis_period_end: string;
  reference_period_start: string;
  reference_period_end: string;
  config: Record<string, unknown>;
  status: ConditionRunStatus;
  error_message: string | null;
  metrics: { signal_produced: boolean; stage?: string; reason?: string } | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  created_by_user_id: string | null;
}

export interface DegradationSignalResponse {
  id: string;
  organization_id: string;
  asset_id: string;
  sensor_id: string;
  condition_run_id: string;
  stage: DegradationStage;
  status: ConditionSignalStatus;
  period_start: string;
  period_end: string;
  summary: string;
  evidence: {
    classification: {
      stage: string | null;
      dominant_direction: string | null;
      sub_windows_agreeing: number;
      sub_window_count: number;
      persistent: boolean;
      corroborated: boolean;
      corroborated_by_deviation: boolean;
      corroborated_by_anomalies: boolean;
      rationale: string;
      sub_window_trends: Array<{ start: string; end: string; observation_count: number; direction: string | null; slope_per_hour: number | null; r_squared: number | null; p_value: number | null; significant: boolean }>;
    };
    deviation_from_baseline: Record<string, unknown>;
    persistent_anomaly_evidence: Record<string, unknown>;
    variance_change_evidence: Record<string, unknown>;
    observation_count: number;
  };
  created_at: string;
  updated_at: string;
}

export interface DegradationReviewResponse {
  id: string;
  degradation_signal_id: string;
  reviewed_by_user_id: string | null;
  from_status: ConditionSignalStatus;
  to_status: ConditionSignalStatus;
  from_stage: DegradationStage;
  to_stage: DegradationStage;
  notes: string | null;
  created_at: string;
}

export interface ForecastEvaluationEntry {
  method: string;
  mae: number | null;
  rmse: number | null;
  points_evaluated: number;
  points_total: number;
  note: string | null;
}

export interface ForecastEvaluationResults {
  computed: boolean;
  reason?: string;
  fit_observation_count?: number;
  test_observation_count?: number;
  evaluations?: Record<string, ForecastEvaluationEntry>;
  is_justified?: boolean;
  justification_note?: string;
  trend_fit_summary?: { slope_per_hour: number; r_squared: number; p_value: number };
  note?: string;
}

export interface ForecastModelResponse {
  id: string;
  organization_id: string;
  sensor_id: string;
  name: string;
  version: number;
  method: "linear_trend";
  config: Record<string, unknown>;
  fit_period_start: string;
  fit_period_end: string;
  fit_observation_count: number;
  status: ForecastModelStatus;
  training_error: string | null;
  fitted_at: string | null;
  evaluation_results: ForecastEvaluationResults | null;
  created_at: string;
  created_by_user_id: string | null;
}

export interface ForecastPoint {
  target_timestamp: string;
  predicted_value: number;
  lower_bound: number;
  upper_bound: number;
}

export interface ForecastRunResponse {
  id: string;
  organization_id: string;
  forecast_model_id: string;
  status: ForecastRunStatus;
  as_of: string;
  horizon_seconds: number;
  step_seconds: number;
  points: ForecastPoint[] | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  created_by_user_id: string | null;
}

export interface AttentionListItem {
  item_type: "degradation_signal" | "anomaly";
  id: string;
  asset_id: string;
  sensor_id: string | null;
  summary: string;
  period_start: string;
  rank: { stage_rank: number; severity_rank: number };
}

export interface AttentionListResult {
  items: AttentionListItem[];
  total_open_degradation_signals: number;
  total_open_anomalies: number;
  sort_methodology: string;
}

export interface ConditionMonitoringSummary {
  degradation_signal_count_by_stage: Record<string, number>;
  degradation_signal_count_by_status: Record<string, number>;
  condition_run_count_by_status: Record<string, number>;
  forecast_model_count_by_status: Record<string, number>;
  not_implemented: string[];
}

// ---------------------------------------------------------------------------
// Phase 5 — Intelligence Lifecycle, Registry & Governance (MLOps)
// ---------------------------------------------------------------------------

export type ComponentType = "trained_ml_model" | "statistical_detector" | "forecasting_model" | "heuristic_signal_method";
export type VersionKind = "model_version" | "forecast_model" | "heuristic";
export type LifecycleStage = "experimenting" | "trained" | "evaluated" | "validated" | "approved" | "active" | "retired" | "failed";
export type GovernanceEventType =
  | "component_registered"
  | "version_registered"
  | "training_initiated"
  | "run_completed"
  | "run_failed"
  | "evaluation_recorded"
  | "validation_decision"
  | "approval_decision"
  | "activated"
  | "rolled_back"
  | "retired"
  | "retraining_requested"
  | "scoring_refused"
  | "shift_analysis_recorded";
export type RetrainingRequestStatus = "requested" | "running" | "evaluated" | "failed" | "rejected";

export interface ComponentResponse {
  id: string;
  organization_id: string;
  component_type: ComponentType;
  name: string;
  description: string | null;
  created_at: string;
  created_by_user_id: string | null;
}

export interface ComponentVersionResponse {
  id: string;
  organization_id: string;
  component_id: string;
  version_number: number;
  version_kind: VersionKind;
  model_version_id: string | null;
  forecast_model_id: string | null;
  heuristic_code_version: string | null;
  lifecycle_stage: LifecycleStage;
  validation_evidence: Record<string, unknown> | null;
  approval_notes: string | null;
  approved_at: string | null;
  approved_by_user_id: string | null;
  activated_at: string | null;
  activated_by_user_id: string | null;
  retired_at: string | null;
  retired_by_user_id: string | null;
  retirement_reason: string | null;
  created_at: string;
  created_by_user_id: string | null;
}

export interface GovernanceEventResponse {
  id: string;
  organization_id: string;
  component_id: string;
  component_version_id: string | null;
  event_type: GovernanceEventType;
  from_stage: LifecycleStage | null;
  to_stage: LifecycleStage | null;
  actor_user_id: string | null;
  details: Record<string, unknown> | null;
  created_at: string;
}

export interface RetrainingRequestResponse {
  id: string;
  organization_id: string;
  component_id: string;
  requested_by_user_id: string | null;
  reason: string;
  config: Record<string, unknown>;
  status: RetrainingRequestStatus;
  resulting_component_version_id: string | null;
  error_message: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface ShiftAnalysisRunResponse {
  id: string;
  organization_id: string;
  sensor_id: string;
  component_id: string | null;
  reference_period_start: string;
  reference_period_end: string;
  comparison_period_start: string;
  comparison_period_end: string;
  method: string;
  result: Record<string, unknown>;
  limitations: string;
  created_at: string;
  created_by_user_id: string | null;
}

export interface MlopsMonitoringSummary {
  operational: {
    component_count_by_type: Record<string, number>;
    component_version_count_by_lifecycle_stage: Record<string, number>;
    retraining_request_count_by_status: Record<string, number>;
    governance_event_count_by_type: Record<string, number>;
    active_components: { component_id: string; name: string; active_version_number: number }[];
  };
  data: { shift_analysis_run_count: number; note: string };
  model_performance: { anomaly_detection: Record<string, unknown>; condition_and_forecasting: Record<string, unknown>; note: string };
  not_implemented: string[];
}

// ---------------------------------------------------------------------------
// Phase 6 — Intelligence Orchestration, Investigations & Decision Support
// ---------------------------------------------------------------------------

export type InvestigationStatus = "open" | "under_review" | "awaiting_context" | "resolved" | "closed";
export type InvestigationEvidenceType =
  | "anomaly"
  | "degradation_signal"
  | "data_quality_issue"
  | "forecast_run"
  | "component_version"
  | "governance_event"
  | "shift_analysis_run"
  | "sensor"
  | "change_candidate";
export type InvestigationAnnotationType = "observation" | "operational_context" | "resolution" | "false_positive_assessment" | "follow_up_required";

export interface InvestigationResponse {
  id: string;
  organization_id: string;
  asset_id: string;
  primary_sensor_id: string | null;
  title: string;
  description: string | null;
  time_window_start: string | null;
  time_window_end: string | null;
  status: InvestigationStatus;
  opened_by_user_id: string | null;
  opened_at: string;
  created_at: string;
  updated_at: string;
}

export interface InvestigationStatusEventResponse {
  id: string;
  investigation_id: string;
  changed_by_user_id: string | null;
  from_status: InvestigationStatus | null;
  to_status: InvestigationStatus;
  notes: string | null;
  created_at: string;
}

export interface EvidenceLinkResponse {
  id: string;
  investigation_id: string;
  evidence_type: InvestigationEvidenceType;
  evidence_id: string | null;
  sensor_id: string | null;
  snapshot: Record<string, unknown> | null;
  relation_note: string | null;
  linked_by_user_id: string | null;
  linked_at: string;
}

export interface AnnotationResponse {
  id: string;
  investigation_id: string;
  author_user_id: string | null;
  annotation_type: InvestigationAnnotationType;
  content: string;
  follow_up_required: boolean;
  created_at: string;
}

export interface RelatedInvestigationResponse {
  id: string;
  investigation_id: string;
  related_investigation_id: string;
  note: string | null;
  linked_by_user_id: string | null;
  created_at: string;
}

export interface TimelineEvent {
  event_type: string;
  category: "observed_data" | "analytical_signal" | "model_output" | "human_interpretation" | "governance";
  timestamp: string;
  sensor_id: string | null;
  summary: string;
  source_type: string;
  source_id: string;
  details: Record<string, unknown>;
}

export interface AssetTimelineResult {
  asset_id: string;
  window: { start: string; end: string };
  events: TimelineEvent[];
  total: number;
  limit: number;
  offset: number;
  available_event_types: string[];
  note: string;
}

export interface PrioritizedSignalItem {
  item_type: "degradation_signal" | "anomaly";
  id: string;
  asset_id: string;
  sensor_id: string | null;
  summary: string;
  period_start: string;
  status: string;
  score: number;
  contributing_factors: {
    stage_rank: number;
    severity_rank: number;
    persistence_count: number;
    cross_detector_agreement: boolean;
    nearby_data_quality_issue_count: number;
    human_reviewed: boolean;
  };
}

export interface PrioritizedSignalsResult {
  items: PrioritizedSignalItem[];
  total_open_degradation_signals: number;
  total_open_anomalies: number;
  methodology_version: string;
  methodology: string;
}

export interface AssetIntelligenceSummary {
  asset_id: string;
  asset_code: string;
  asset_name: string;
  sensor_count: number;
  observed_data: { total_data_quality_issues: number; issues_by_severity: Record<string, number> };
  analytical_signals: { open_degradation_signal_count: number; degradation_stage_breakdown: Record<string, number>; change_candidate_note: string };
  model_outputs: { open_anomaly_count: number; anomaly_severity_breakdown: Record<string, number>; detector_types_active: string[]; forecast_models_fitted: number; forecast_models_justified: number };
  human_interpretations: { investigation_count: number; investigations_by_status: Record<string, number>; annotation_count: number; unreviewed_anomaly_count: number; unreviewed_degradation_signal_count: number };
  limitations: string[];
}

export interface CrossSensorContextResult {
  asset_id: string;
  sensor_ids: string[];
  window: { start: string; end: string; freq_seconds: number };
  correlation: Record<string, unknown>;
  per_sensor_signals: Record<string, { open_anomaly_count: number; open_degradation_signal_count: number; anomaly_periods: Record<string, unknown>[] }>;
  observed_associations: string[];
  note: string;
}

export interface EvidenceProvenanceResult {
  evidence_type: string;
  evidence_id: string;
  summary: Record<string, unknown>;
  technical: Record<string, unknown>;
}

export interface JobResponse {
  id: string;
  job_type: string;
  status: JobStatus;
  progress: number;
  result: Record<string, unknown> | null;
  error_message: string | null;
  correlation_id: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

// ---------------------------------------------------------------------------
// AI Copilot (Phase 9) — see docs/COPILOT.md. The copilot is OPTIONAL and
// provider-dependent: `CopilotStatusResponse.configured` tells the frontend
// whether natural-language answers are available; the retrieved evidence
// itself is always real and always shown either way, never a placeholder.
// ---------------------------------------------------------------------------

export type CopilotContextType = "general" | "asset" | "investigation" | "anomaly" | "degradation_signal" | "scenario" | "outcome";

/** The five evidence categories the copilot never blurs together — distinct
 * from (though conceptually aligned with) IntelligenceWorkspacePage's own
 * observed_data/analytical_signal/model_output/human_interpretation/governance
 * taxonomy. Kept as its own set because the copilot also needs a category
 * for "the evidence doesn't establish this" (interpretation_limitation),
 * which has no single equivalent among the workspace's five. */
export type CopilotEvidenceCategory =
  | "observed_fact"
  | "analytical_result"
  | "model_output"
  | "human_annotation"
  | "interpretation_limitation";

export interface CopilotEvidenceItem {
  ref: string;
  category: CopilotEvidenceCategory;
  source_type: string;
  source_id: string | null;
  label: string;
  period_start: string | null;
  period_end: string | null;
  method_version: string | null;
  text: string;
}

export interface CopilotConversationResponse {
  id: string;
  organization_id: string;
  user_id: string;
  context_type: CopilotContextType;
  context_id: string | null;
  title: string | null;
  created_at: string;
}

export type CopilotMessageRole = "user" | "assistant";

export interface CopilotMessageResponse {
  id: string;
  conversation_id: string;
  role: CopilotMessageRole;
  content: string;
  grounding: CopilotEvidenceItem[] | null;
  provider_used: string | null;
  flagged_ungrounded: boolean;
  created_at: string;
}

export interface CopilotStatusResponse {
  provider: string;
  configured: boolean;
  model: string | null;
  note: string;
}

export interface CopilotSuggestedQuestionsResponse {
  context_type: CopilotContextType;
  context_id: string | null;
  questions: string[];
}

export interface CopilotEvidenceSelection {
  kind: "asset" | "anomaly" | "investigation" | "sensor" | "data_quality" | "scenario" | "outcome" | "evaluation";
  id: string;
}

// ---------------------------------------------------------------------------
// Scenario Simulation / What-If Analysis (Phase 11)
//
// THIS IS SCENARIO ANALYSIS, NOT A PERFECT PHYSICAL DIGITAL TWIN. Every
// scenario result below is a hypothetical, non-destructive what-if built by
// re-running the SAME real analytical pipeline (robust z-score, condition
// evidence/degradation, change detection, forecast backtest) against a
// perturbed copy of real historical data. Result payloads are loosely typed
// (Record<string, unknown> / `any`-ish shapes) because they are validated
// and produced by app.simulation.* on the backend, not by this schema layer
// — mirroring how condition/anomaly "evidence" blobs are treated elsewhere
// in this file. Every result the UI renders must stay visibly labeled
// SIMULATED and distinct from real observations — see ScenarioLabPage.tsx.
// ---------------------------------------------------------------------------

export type ScenarioType = "level_shift" | "trend_change" | "variability_change" | "temporary_spike" | "drift" | "missing_data_gap" | "multi_sensor";
export type ScenarioStatus = "draft" | "validated" | "archived";
export type ScenarioRunStatus = "queued" | "running" | "succeeded" | "failed";
export type ScenarioRunKind = "single" | "sensitivity_sweep";

export interface ScenarioValidationResult {
  ok: boolean;
  errors: string[];
  warnings: string[];
  checked_at: string | null;
}

export interface ScenarioResponse {
  id: string;
  organization_id: string;
  asset_id: string;
  sensor_id: string;
  sensor_scope: string[];
  name: string;
  description: string | null;
  scenario_type: ScenarioType;
  baseline_window_start: string;
  baseline_window_end: string;
  reference_window_start: string | null;
  reference_window_end: string | null;
  modification_start: string | null;
  modification_duration_seconds: number | null;
  magnitude: number | null;
  unit: string | null;
  parameters: Record<string, unknown>;
  status: ScenarioStatus;
  version: number;
  validation_result: ScenarioValidationResult | null;
  created_by_user_id: string | null;
  created_at: string;
  updated_at: string;
}

/** One value_summary/anomaly/condition/change/forecast bundle, produced by
 * app.simulation.pipeline.run_series_through_pipeline for either the real
 * baseline series or its simulated counterpart. `label` always says
 * "SIMULATED SCENARIO RESULT" or "OBSERVED (real sensor data)". */
export interface ScenarioPipelineBundle {
  label: string;
  is_simulated: boolean;
  observation_count: number;
  value_summary: { mean: number | null; median: number | null; std: number | null; min: number | null; max: number | null };
  anomaly_detection: Record<string, unknown>;
  condition_intelligence: Record<string, unknown>;
  change_candidates: Record<string, unknown>;
  forecast_backtest?: Record<string, unknown>;
}

export interface ScenarioSingleResult {
  run_type: "single";
  baseline: ScenarioPipelineBundle;
  simulated: ScenarioPipelineBundle;
  comparison: Record<string, unknown>;
  limitations: string[];
  provenance: Record<string, unknown>;
}

export interface ScenarioMultiSensorResult {
  run_type: "multi_sensor";
  per_sensor: Record<string, { baseline: ScenarioPipelineBundle; simulated: ScenarioPipelineBundle; comparison: Record<string, unknown>; limitations: string[]; provenance: Record<string, unknown> }>;
  limitations: string[];
  provenance: Record<string, unknown>;
}

export interface ScenarioSensitivitySweepResult {
  run_type: "sensitivity_sweep";
  sensitivity: { magnitudes_tested: number[]; points: Array<{ magnitude: number; comparison: Record<string, unknown> }>; note: string };
  limitations: string[];
  provenance: Record<string, unknown>;
}

export type ScenarioRunResults = ScenarioSingleResult | ScenarioMultiSensorResult | ScenarioSensitivitySweepResult | Record<string, unknown>;

export interface ScenarioRunResponse {
  id: string;
  organization_id: string;
  scenario_id: string;
  scenario_version: number;
  run_kind: ScenarioRunKind;
  status: ScenarioRunStatus;
  config_snapshot: Record<string, unknown>;
  pipeline_versions: Record<string, unknown>;
  results: ScenarioRunResults | null;
  error_message: string | null;
  background_job_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  created_by_user_id: string | null;
}

export interface ScenarioCompareResponse {
  data: {
    runs: Array<{ scenario_run_id: string; scenario_id: string; scenario_version: number; run_kind: ScenarioRunKind; results: ScenarioRunResults | null; created_at: string }>;
    note: string;
  };
}

// ---------------------------------------------------------------------------
// Closed-loop outcome, verification, and evaluation (Phase 12)
// ---------------------------------------------------------------------------

export type OutcomeType = "maintenance_event" | "operational_event" | "inspection_result" | "failure_event" | "recovery_event" | "other";
export type VerificationStatus = "unverified" | "reviewed" | "verified" | "rejected" | "conflicting";
export type OutcomeSignalType = "anomaly" | "degradation_signal" | "forecast_run" | "scenario_run";
export type SignalAssessment = "not_yet_assessed" | "confirms" | "refutes" | "inconclusive";
export type EvaluationKind = "initial" | "updated";
export type RetrainingCandidateStatus = "proposed" | "under_review" | "approved" | "dismissed";
export type FeedbackDatasetStatus = "built" | "failed";
export type PrioritizationPolicyStatus = "draft" | "active" | "retired";

export interface OutcomeResponse {
  id: string;
  asset_id: string;
  sensor_id: string | null;
  investigation_id: string | null;
  outcome_type: OutcomeType;
  source: "user_reported";
  description: string;
  observation_period_start: string | null;
  observation_period_end: string | null;
  outcome_timestamp: string;
  verification_status: VerificationStatus;
  reported_by_user_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface OutcomeVerificationResponse {
  id: string;
  outcome_id: string;
  from_status: VerificationStatus;
  to_status: VerificationStatus;
  reviewer_user_id: string | null;
  reviewer_role: string | null;
  evidence_note: string | null;
  created_at: string;
}

export interface OutcomeSignalLinkResponse {
  id: string;
  outcome_id: string;
  signal_type: OutcomeSignalType;
  signal_id: string;
  sensor_id: string | null;
  component_id: string | null;
  prediction_timestamp: string;
  horizon_seconds: number;
  within_horizon: boolean;
  counts_toward_evaluation: boolean;
  signal_assessment: SignalAssessment;
  assessed_by_user_id: string | null;
  assessed_at: string | null;
  linked_by_user_id: string | null;
  linked_at: string;
  notes: string | null;
}

export interface OutcomeEvaluationMetrics {
  computed: boolean;
  reason: string | null;
  precision: number | null;
  true_positive_count: number;
  false_positive_count: number;
  unresolved_count: number;
  eligible_count: number;
  min_required: number;
  coverage_classification: "no_data" | "no_coverage" | "partial_coverage" | "full_coverage";
  recall_note: string;
}

export interface OutcomeEvaluationRunResponse {
  id: string;
  component_id: string | null;
  sensor_id: string | null;
  signal_type: OutcomeSignalType;
  evaluation_kind: EvaluationKind;
  previous_evaluation_run_id: string | null;
  scope_description: string;
  evaluation_period_start: string;
  evaluation_period_end: string;
  computed_at: string;
  total_signals_in_scope: number;
  linked_outcome_count: number;
  verified_outcome_count: number;
  coverage_fraction: number | null;
  metrics: OutcomeEvaluationMetrics;
  created_by_user_id: string | null;
}

export interface RetrainingCandidateResponse {
  id: string;
  component_id: string;
  supporting_evaluation_run_id: string | null;
  trigger_reason: string;
  status: RetrainingCandidateStatus;
  reviewed_by_user_id: string | null;
  review_notes: string | null;
  reviewed_at: string | null;
  resulting_retraining_request_id: string | null;
  created_by_user_id: string | null;
  created_at: string;
}

export interface FeedbackDatasetBuildResponse {
  id: string;
  name: string;
  definition: Record<string, unknown>;
  status: FeedbackDatasetStatus;
  dataset_version: number;
  row_count: number;
  rows: Array<Record<string, unknown>>;
  error_message: string | null;
  built_at: string | null;
  created_by_user_id: string | null;
  created_at: string;
}

export interface FeedbackDatasetBuildSummaryResponse {
  id: string;
  name: string;
  definition: Record<string, unknown>;
  status: FeedbackDatasetStatus;
  dataset_version: number;
  row_count: number;
  error_message: string | null;
  built_at: string | null;
  created_by_user_id: string | null;
  created_at: string;
}

export interface PrioritizationPolicyResponse {
  id: string;
  version: number;
  status: PrioritizationPolicyStatus;
  config: Record<string, unknown>;
  rationale: string;
  activated_at: string | null;
  activated_by_user_id: string | null;
  retired_at: string | null;
  created_by_user_id: string | null;
  created_at: string;
}

export interface ErrorAnalysisReport {
  late_detections: Array<{ link_id: string; signal_id: string; detection_delay_seconds: number }>;
  duplicate_candidates: Array<{ sensor_id: string; signal_type: string; signal_ids: string[] }>;
  false_negative_candidates: Array<{ outcome_id: string; sensor_id: string | null; reason: string }>;
  late_detection_count: number;
  duplicate_candidate_count: number;
  false_negative_candidate_count: number;
  note: string;
}

export interface ClosedLoopAnalytics {
  outcomes_by_verification_status: Record<VerificationStatus, number>;
  links_by_signal_assessment: Record<SignalAssessment, number>;
  evaluation_run_count: number;
  retraining_candidates_by_status: Record<RetrainingCandidateStatus, number>;
  feedback_dataset_build_count: number;
  active_prioritization_policy_id: string | null;
  note: string;
}

export interface LabelDefinitions {
  version: number;
  verification_status: Record<string, string>;
  signal_assessment: Record<string, string>;
  classification_rules: Record<string, string>;
}

"""Explicit, versioned, human-readable label governance definitions
(Phase 12 §18).

Every status and classification this platform's closed-loop machinery uses
is defined here in plain language and exposed through the
`/feedback/label-definitions` API endpoint, so "what does VERIFIED actually
mean on this platform" or "what counts as a false positive here" is always
an answerable, auditable question — not something only readable by reading
service-layer code.

`LABEL_DEFINITIONS_VERSION` is bumped whenever any definition's *meaning*
changes (wording-only fixes do not need a bump). A consumer of
`/feedback/label-definitions` that caches these definitions should key its
cache on this version.
"""

LABEL_DEFINITIONS_VERSION = 1

VERIFICATION_STATUS_DEFINITIONS: dict[str, str] = {
    "unverified": (
        "A human reported this outcome, but no one has reviewed or confirmed it yet. "
        "This is the only state an outcome can be created in — reporting is never itself verification."
    ),
    "reviewed": (
        "A human has looked at this outcome and recorded that they reviewed it, but has not yet "
        "made a final verified/rejected determination. An intermediate, non-final state."
    ),
    "verified": (
        "A human has explicitly confirmed this outcome is accurate, and recorded evidence supporting "
        "that confirmation. Only VERIFIED outcomes (together with an explicit signal_assessment) count "
        "toward evaluation metrics."
    ),
    "rejected": (
        "A human has explicitly determined this outcome report was inaccurate, mistaken, or should not "
        "be trusted, and recorded evidence supporting that rejection."
    ),
    "conflicting": (
        "Different reviewers reached different conclusions about this outcome and that disagreement is "
        "preserved rather than letting the most recent decision silently overwrite the earlier one. "
        "A CONFLICTING outcome does not count toward evaluation metrics until it is resolved back to "
        "VERIFIED or REJECTED."
    ),
}

SIGNAL_ASSESSMENT_DEFINITIONS: dict[str, str] = {
    "not_yet_assessed": (
        "No human has yet judged whether this outcome confirms or refutes the signal it is linked to. "
        "This is the default state for every new link."
    ),
    "confirms": (
        "A human has explicitly judged that this verified outcome confirms the signal was correct — "
        "i.e., what the signal flagged did, in fact, happen."
    ),
    "refutes": (
        "A human has explicitly judged that this verified/rejected outcome refutes the signal — "
        "i.e., what the signal flagged did not, in fact, happen."
    ),
    "inconclusive": (
        "A human looked at the outcome and the signal together and determined the evidence does not "
        "clearly confirm or refute it either way."
    ),
}

CLASSIFICATION_RULES: dict[str, str] = {
    "true_positive": (
        "A link whose outcome is VERIFIED, whose signal_assessment is CONFIRMS, and whose outcome "
        "timestamp fell within the signal's frozen prediction horizon. The signal flagged something "
        "real, in time."
    ),
    "false_positive": (
        "A link whose outcome is VERIFIED or REJECTED and whose signal_assessment is REFUTES. "
        "A human explicitly determined the signal did not correspond to anything real."
    ),
    "unresolved": (
        "Any link that does not yet meet the true_positive or false_positive rule above — including "
        "NOT_YET_ASSESSED links, INCONCLUSIVE links, and links whose outcome is still UNVERIFIED, "
        "REVIEWED, or CONFLICTING. Unresolved links are excluded from precision calculations; they are "
        "not silently counted as either correct or incorrect."
    ),
    "false_negative_candidate": (
        "Not derived from a signal-outcome link at all (there is none). A verified, fault-like outcome "
        "(e.g. a FAILURE_EVENT) with no outcome-signal link pointing to it is flagged as a *candidate* "
        "false negative for human review — never asserted as a confirmed missed detection, because this "
        "platform cannot automatically rule out a non-monitored cause."
    ),
}


def get_label_definitions() -> dict:
    """Assembled payload for the `/feedback/label-definitions` endpoint."""
    return {
        "version": LABEL_DEFINITIONS_VERSION,
        "verification_status": VERIFICATION_STATUS_DEFINITIONS,
        "signal_assessment": SIGNAL_ASSESSMENT_DEFINITIONS,
        "classification_rules": CLASSIFICATION_RULES,
    }

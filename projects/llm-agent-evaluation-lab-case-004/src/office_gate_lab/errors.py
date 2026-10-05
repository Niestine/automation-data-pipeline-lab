"""Lab errors that callers can distinguish from bugs."""


class LabError(Exception):
    """Base class for expected lab failures."""


class SchemaAdmissionError(LabError):
    """A schema uses a keyword outside the strict dialect."""


class CassetteMiss(LabError):
    """No recorded completion exists for this request hash."""


class JudgeGateError(LabError):
    """The LLM judge is held, or the rubric hash has no agreement."""


class LabInputError(LabError):
    """A fixture path is missing or malformed."""

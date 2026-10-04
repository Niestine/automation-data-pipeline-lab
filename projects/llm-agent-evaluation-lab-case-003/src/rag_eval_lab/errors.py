"""Project errors for the RAG evaluation lab."""


class LabError(Exception):
    """A scored run or report violated a lab invariant."""


class LabInputError(Exception):
    """An example file or payload is missing or malformed."""

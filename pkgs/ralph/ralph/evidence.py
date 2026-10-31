"""Failures known by evidence tools, independent of model interpretation."""


class ToolFailure(Exception):
    def __init__(self, outcome, message):
        super().__init__(message)
        self.outcome = outcome

from __future__ import annotations


class AgentHubError(Exception):
    status_code = 400


class NotFound(AgentHubError):
    status_code = 404


class Forbidden(AgentHubError):
    status_code = 403


class Conflict(AgentHubError):
    status_code = 409


class InvalidTransition(AgentHubError):
    status_code = 409


class BudgetExceeded(AgentHubError):
    status_code = 429


class ProviderError(AgentHubError):
    status_code = 502


class AgentUnavailable(AgentHubError):
    status_code = 503

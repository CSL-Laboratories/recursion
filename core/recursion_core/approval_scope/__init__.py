"""Domain-neutral approval prompt and approval scope (CSL-722; O74, I-42).

No consumer-domain names here: the core binds approvals to a canonical digest and shows
caller fields as opaque labelled text. Consumers filter their own domain reads.
"""

from .errors import Refused
from .prompt import ApprovalRequest, render_prompt, sanitize
from .scope import ApprovalAuthority, ApprovalScope, Credential

__all__ = ["ApprovalAuthority", "ApprovalRequest", "ApprovalScope", "Credential", "Refused",
           "render_prompt", "sanitize"]

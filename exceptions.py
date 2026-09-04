class SovereignError(Exception): pass
class VaultError(SovereignError): pass
class PathTraversalError(VaultError): pass
class ToolError(SovereignError): pass
class MoltbookError(ToolError): pass

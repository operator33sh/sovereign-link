"""Entry point: python -m mcp_server"""
from mcp_server.server import server

server.run()  # defaults to stdio transport

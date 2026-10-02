"""Tool router and manifest registry.

Classifies tools into read-only vs. state-modifying (§4),
validates arguments against tool schemas, and manages tool execution hooks.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine, Dict, Optional
import jsonschema


class ToolDefinition:
    def __init__(
        self,
        name: str,
        description: str,
        parameters_schema: Dict[str, Any],
        is_state_modifying: bool = False,
        handler: Optional[Callable[..., Coroutine[Any, Any, Any]]] = None,
    ):
        self.name = name
        self.description = description
        self.parameters_schema = parameters_schema
        self.is_state_modifying = is_state_modifying
        self.handler = handler

    def validate_args(self, arguments: Dict[str, Any]) -> None:
        """Validate argument dictionary against parameter schema."""
        if self.parameters_schema:
            jsonschema.validate(instance=arguments, schema=self.parameters_schema)


class UnknownToolError(KeyError):
    """The model asked for a tool that is not registered."""


class ToolRouter:
    def __init__(self, register_defaults: bool = True):
        self._tools: Dict[str, ToolDefinition] = {}
        if register_defaults:
            self.register_default_tools()

    def register_default_tools(self) -> None:
        """Register standard tools for flights, hotels, weather, and bookings."""
        async def _search_flights(origin: str, destination: str, date: str = "today", **kwargs):
            await asyncio.sleep(3.0)  # Realistic async latency to allow observation & interruption
            return {
                "flights": [
                    {"flight": "AI-102", "airline": "Air India", "origin": origin, "destination": destination, "price": "$180", "departure": "08:30 AM"},
                    {"flight": "6E-455", "airline": "IndiGo", "origin": origin, "destination": destination, "price": "$145", "departure": "11:15 AM"},
                    {"flight": "UK-820", "airline": "Vistara", "origin": origin, "destination": destination, "price": "$195", "departure": "03:45 PM"},
                ]
            }

        async def _book_flight(origin: str, destination: str, flight: str = "6E-455", **kwargs):
            await asyncio.sleep(2.5)
            return {"booking_id": "FL-98214", "status": "confirmed", "origin": origin, "destination": destination, "flight": flight}

        async def _search_hotels(city: str, nights: int = 2, **kwargs):
            await asyncio.sleep(3.0)
            return {
                "hotels": [
                    {"name": f"The Grand {city}", "stars": 5, "price_per_night": "$140", "rating": 4.8},
                    {"name": f"{city} Downtown Suites", "stars": 4, "price_per_night": "$95", "rating": 4.5},
                ]
            }

        async def _book_hotel(city: str, hotel_name: str = "Downtown Suites", nights: int = 2, **kwargs):
            await asyncio.sleep(2.5)
            return {"reservation_id": "HT-44012", "status": "confirmed", "hotel": hotel_name, "city": city, "nights": nights}

        async def _check_weather(city: str, **kwargs):
            await asyncio.sleep(2.0)
            return {"city": city, "condition": "Sunny", "temp": "28°C", "humidity": "45%"}

        async def _cancel_booking(booking_id: str, **kwargs):
            await asyncio.sleep(2.0)
            return {"status": "cancelled", "booking_id": booking_id, "refund": "processed"}

        self.register_tool(
            name="search_flights",
            description="Search available flights between origin and destination cities",
            parameters_schema={
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "Origin city or airport code (e.g. BLR, Delhi)"},
                    "destination": {"type": "string", "description": "Destination city or airport code (e.g. DEL, Mumbai)"},
                    "date": {"type": "string", "description": "Travel date"},
                },
                "required": ["origin", "destination"],
            },
            is_state_modifying=False,
            handler=_search_flights,
        )

        self.register_tool(
            name="book_flight",
            description="Book a flight reservation (state-modifying). Needs only origin and destination (flight is optional); do not ask the user for a date or time first.",
            parameters_schema={
                "type": "object",
                "properties": {
                    "origin": {"type": "string"},
                    "destination": {"type": "string"},
                    "flight": {"type": "string"},
                },
                "required": ["origin", "destination"],
            },
            is_state_modifying=True,
            handler=_book_flight,
        )

        self.register_tool(
            name="search_hotels",
            description="Search available hotels in a destination city",
            parameters_schema={
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                    "nights": {"type": "integer", "description": "Number of nights"},
                },
                "required": ["city"],
            },
            is_state_modifying=False,
            handler=_search_hotels,
        )

        self.register_tool(
            name="book_hotel",
            description="Book a hotel room (state-modifying)",
            parameters_schema={
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "hotel_name": {"type": "string"},
                    "nights": {"type": "integer"},
                },
                "required": ["city"],
            },
            is_state_modifying=True,
            handler=_book_hotel,
        )

        self.register_tool(
            name="check_weather",
            description="Check weather conditions in a city",
            parameters_schema={
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
            is_state_modifying=False,
            handler=_check_weather,
        )

        self.register_tool(
            name="cancel_booking",
            description="Cancel an active booking or reservation (state-modifying)",
            parameters_schema={
                "type": "object",
                "properties": {"booking_id": {"type": "string"}},
                "required": ["booking_id"],
            },
            is_state_modifying=True,
            handler=_cancel_booking,
        )

        async def _spawn_agent(name: str = "bob", role: str = "Autonomous Worker", goal: str = "Execute task", **kwargs):
            # Handled directly by coordinator execution engine for streaming
            return {"status": "completed", "agent_name": name, "role": role, "goal": goal}

        async def _analyze_frame(question: str = "Describe what you see.", **kwargs):
            # Executed by the coordinator, which owns the per-session frame buffer and the vision backend.
            return {"answer": "", "has_frame": False}

        self.register_tool(
            name="analyze_frame",
            description=(
                "Look at the user's shared camera or screen frame and answer a question about it. Use when the user "
                "refers to something visible (\"this\", \"on my screen\", \"in the picture\", \"the sign\")."
            ),
            parameters_schema={
                "type": "object",
                "properties": {"question": {"type": "string", "description": "What you need to know from the image"}},
                "required": ["question"],
            },
            is_state_modifying=False,
            handler=_analyze_frame,
        )

        self.register_tool(
            name="spawn_agent",
            description="Synthesize and spawn a bespoke autonomous agent with custom persona, plan, and artifact requirements",
            parameters_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Agent name, e.g. db_architect, vector_craft, sec_auditor, bob, scout"},
                    "role": {"type": "string", "description": "Agent specialization title and persona, e.g. PostgreSQL Scaling Specialist, SVG Motion Engineer"},
                    "goal": {"type": "string", "description": "Exact objective and task requirements for the agent"},
                    "system_prompt": {"type": "string", "description": "Custom system instructions and domain expertise defining this agent's persona"},
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Step-by-step thinking plan tailored to this exact request",
                    },
                    "expected_artifact": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string", "description": "File or artifact name, e.g. schema.sql, Gauge.svg, audit.md"},
                            "language": {"type": "string", "description": "Syntax language, e.g. sql, svg, python, markdown, typescript"},
                        },
                        "description": "Bespoke artifact specifications",
                    },
                    "component": {"type": "string", "description": "Target component or entity name if applicable"},
                    "language": {"type": "string", "description": "Programming language if code-related"},
                },
                "required": ["name", "role", "goal"],
            },
            is_state_modifying=False,
            handler=_spawn_agent,
        )



    def register_tool(
        self,
        name: str,
        description: str,
        parameters_schema: Dict[str, Any],
        is_state_modifying: bool = False,
        handler: Optional[Callable[..., Coroutine[Any, Any, Any]]] = None,
    ) -> None:
        """Register a tool definition into the router."""
        tool_def = ToolDefinition(
            name=name,
            description=description,
            parameters_schema=parameters_schema,
            is_state_modifying=is_state_modifying,
            handler=handler,
        )
        self._tools[name] = tool_def

    def is_state_modifying(self, tool_name: str) -> bool:
        """Determine if a tool call alters state and requires idempotency tracking."""
        tool = self._tools.get(tool_name)
        if not tool:
            # Conservative default: assume unknown tool might modify state if named accordingly
            state_verbs = ("book", "cancel", "delete", "create", "update", "buy", "pay", "send")
            return any(tool_name.lower().startswith(v) for v in state_verbs)
        return tool.is_state_modifying

    def validate_call(self, tool_name: str, arguments: Dict[str, Any]) -> None:
        """Validate tool arguments against registered manifest schema."""
        tool = self._tools.get(tool_name)
        if tool is None:
            raise UnknownToolError(f"no tool named {tool_name!r}")
        tool.validate_args(arguments)

    def get_tool_manifests(self) -> Dict[str, Any]:
        """Return schema manifests formatted for LLM function calling."""
        manifests = []
        for name, tool in self._tools.items():
            manifests.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.description,
                    "parameters": tool.parameters_schema,
                },
                "is_state_modifying": tool.is_state_modifying,
            })
        return manifests

    async def execute_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """Execute the registered handler for the tool."""
        tool = self._tools.get(tool_name)
        if not tool or not tool.handler:
            raise ValueError(f"No execution handler registered for tool '{tool_name}'")
        return await tool.handler(**arguments)

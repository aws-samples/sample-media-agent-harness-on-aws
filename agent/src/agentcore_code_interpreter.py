# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software distributed under the License is
# distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing permissions and limitations under the
# License.
#
# Vendored from strands-agents-tools 0.8.7 (code_interpreter/models.py, code_interpreter.py and
# agent_core_code_interpreter.py; Apache-2.0), trimmed to what this harness uses. See NOTICE.
"""The ``code_interpreter`` tool on the AgentCore Code Interpreter.

Why it is vendored
------------------
``strands-agents-tools`` is no longer a dependency: its 0.9.0 is the deprecation release (every tool
import raises), it gets no further updates, and it held Pillow below 13. This is the only part of it
the harness still needs, kept BEHAVIOUR-IDENTICAL on purpose:

* the model-facing tool (name ``code_interpreter``, the ``CodeInterpreterInput`` schema with its class
  docstrings and field descriptions, and the description text) is what 0.8.7 produced, byte for byte
  (``tests/test_agentcore_code_interpreter.py`` pins its digest), so prompts, prompt caching and the
  ``sandbox_text`` rewrites see no change;
* the private bookkeeping that ``tool_input_normalize.NormalizedCodeInterpreterTool`` and
  ``sandbox_scope`` reach into keeps its names: ``AgentCoreCodeInterpreter._sessions`` (name ->
  :class:`SessionInfo` with ``session_id`` and ``client``), ``_ensure_session(name) -> (name, error)``,
  ``default_session``, and the module-level :data:`_session_mapping`.

Trimmed: the long docstrings and the abstract base methods (there is one implementation). The sandbox
client is ``bedrock_agentcore``'s ``CodeInterpreter``, as before.
"""

import logging
import os
import uuid
from abc import ABC
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

import boto3
from bedrock_agentcore.tools.code_interpreter_client import CodeInterpreter as BedrockAgentCoreCodeInterpreterClient
from pydantic import BaseModel, Field, model_validator
from strands import tool

logger = logging.getLogger(__name__)

# Module-level session cache - persists across object instances
_session_mapping: Dict[str, str] = {}  # user_session_name -> aws_session_id

# ---------------------------------------------------------------------------------------------------
# Models (verbatim: their docstrings and Field descriptions ARE the tool's input schema)
# ---------------------------------------------------------------------------------------------------


class LanguageType(str, Enum):
    """Supported programming languages for code execution."""

    PYTHON = "python"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"


class FileContent(BaseModel):
    """Represents a file with its path and text content for writing to the sandbox file system. Used when creating or
    updating files during code execution sessions."""

    path: str = Field(description="The file path where content should be written")
    text: Optional[str] = Field(
        default=None,
        description="Text content for the file",
    )
    blob: Optional[bytes] = Field(
        default=None,
        description="Base64-encoded binary content for the file",
    )

    @model_validator(mode="after")
    def validate_content(self) -> "FileContent":
        if self.text is None and self.blob is None:
            raise ValueError("Either text or blob must be provided")
        if self.text is not None and self.blob is not None:
            raise ValueError("Only one of text or blob may be provided")
        return self


# Action-specific Pydantic models using discriminated unions
class InitSessionAction(BaseModel):
    """Create a new isolated code execution environment. Use this when starting a new coding task, data analysis
    project, or when you need a fresh sandbox environment. Each session maintains its own state, variables,
    and file system."""

    type: Literal["initSession"] = Field(description="Initialize a new code interpreter session")
    description: str = Field(description="Required description of what this session will be used for")
    session_name: Optional[str] = Field(
        default=None, description="Session name. If not provided, a default session will be used."
    )


class ListLocalSessionsAction(BaseModel):
    """View all active code interpreter sessions managed by this tool instance. Use this to see what sessions are
    available, check their status, or find the session name you need for other operations."""

    type: Literal["listLocalSessions"] = Field(description="List all local sessions managed by this tool instance")


class ExecuteCodeAction(BaseModel):
    """Execute code in a specific programming language within an existing session. Use this for running Python
    scripts, JavaScript/TypeScript code, data analysis, calculations, or any programming task. The session maintains
    state between executions."""

    type: Literal["executeCode"] = Field(description="Execute code in the code interpreter")

    session_name: Optional[str] = Field(
        default=None,
        description="Session name. If not provided, uses the default session which will be auto-created if needed.",
    )

    code: str = Field(description="Required code to execute")
    language: LanguageType = Field(default=LanguageType.PYTHON, description="Programming language for code execution")
    clear_context: bool = Field(default=False, description="Whether to clear the execution context before running code")


class ExecuteCommandAction(BaseModel):
    """Execute shell/terminal commands within the sandbox environment. Use this for system operations like installing
    packages, running scripts, file management, or any command-line tasks that need to be performed in the session."""

    type: Literal["executeCommand"] = Field(description="Execute a shell command in the code interpreter")

    session_name: Optional[str] = Field(
        default=None, description="Session name. If not provided, uses the default session."
    )

    command: str = Field(description="Required shell command to execute")


class ReadFilesAction(BaseModel):
    """Read the contents of one or more files from the sandbox file system. Use this to examine data files,
    configuration files, code files, or any other files that have been created or uploaded to the session."""

    type: Literal["readFiles"] = Field(description="Read files from the code interpreter")

    session_name: Optional[str] = Field(
        default=None, description="Session name. If not provided, uses the default session."
    )

    paths: List[str] = Field(description="List of file paths to read")


class ListFilesAction(BaseModel):
    """Browse and list files and directories within the sandbox file system. Use this to explore the directory
    structure, find files, or understand what's available in the session before reading or manipulating files."""

    type: Literal["listFiles"] = Field(description="List files in a directory")

    session_name: Optional[str] = Field(
        default=None, description="Session name. If not provided, uses the default session."
    )

    path: str = Field(default=".", description="Directory path to list (defaults to current directory)")


class RemoveFilesAction(BaseModel):
    """Delete one or more files from the sandbox file system. Use this to clean up temporary files, remove outdated
    data, or manage storage space within the session. Be careful as this permanently removes files."""

    type: Literal["removeFiles"] = Field(description="Remove files from the code interpreter")

    session_name: Optional[str] = Field(
        default=None, description="Session name. If not provided, uses the default session."
    )

    paths: List[str] = Field(description="Required list of file paths to remove")


class WriteFilesAction(BaseModel):
    """Create or update multiple files in the sandbox file system with specified content. Use this to save data,
    create configuration files, write code files, or store any text-based content that your code execution will need."""

    type: Literal["writeFiles"] = Field(description="Write files to the code interpreter")

    session_name: Optional[str] = Field(
        default=None, description="Session name. If not provided, uses the default session."
    )

    content: List[FileContent] = Field(description="Required list of file content to write")


class CodeInterpreterInput(BaseModel):
    action: Union[
        InitSessionAction,
        ListLocalSessionsAction,
        ExecuteCodeAction,
        ExecuteCommandAction,
        ReadFilesAction,
        ListFilesAction,
        RemoveFilesAction,
        WriteFilesAction,
    ] = Field(discriminator="type")


# ---------------------------------------------------------------------------------------------------
# The tool (the description text is verbatim)
# ---------------------------------------------------------------------------------------------------


class CodeInterpreter(ABC):
    def __init__(self):
        self._started = False

        # Existing description (keep all of it)
        description_template = """
        Code Interpreter tool for executing code in isolated sandbox environments.

        This tool provides a comprehensive code execution platform that supports multiple programming
        languages with persistent session management, file operations, and shell command execution. 
        Built on the Bedrock AgentCore Code Sandbox platform, it offers secure, isolated environments 
        for code execution with full lifecycle management.

        Key Features:
        1. Multi-Language Support:
           The tool supports the following programming languages: {supported_languages_list}
           • Full standard library access for each supported language
           • Runtime environment appropriate for each language
           • Shell command execution for system operations

        2. Session Management:
           • Create named, persistent sessions for stateful code execution
           • List and manage multiple concurrent sessions
           • Automatic session cleanup and resource management
           • Session isolation for security and resource separation

        3. File System Operations:
           • Read files from the sandbox environment
           • Write multiple files with custom content
           • List directory contents and navigate file structures
           • Remove files and manage sandbox storage

        4. Advanced Execution Features:
           • Context preservation across code executions within sessions
           • Optional context clearing for fresh execution environments
           • Real-time output capture and error handling
           • Support for long-running processes and interactive code

        How It Works:
        ------------
        1. The tool accepts structured action inputs defining the operation type
        2. Sessions are created on-demand with isolated sandbox environments
        3. Code is executed within the Bedrock AgentCore platform with full runtime support
        4. Results, outputs, and errors are captured and returned in structured format
        5. File operations interact directly with the sandbox file system
        6. Platform lifecycle is managed automatically with cleanup on completion

        Operation Types:
        --------------
        - initSession: Create a new isolated code execution session
        - listLocalSessions: View all active sessions and their status
        - executeCode: Run code in a specified programming language
        - executeCommand: Execute shell commands in the sandbox
        - readFiles: Read file contents from the sandbox file system
        - writeFiles: Create or update files in the sandbox
        - listFiles: Browse directory contents and file structures
        - removeFiles: Delete files from the sandbox environment

        Common Usage Scenarios:
        ---------------------
        - Data analysis: Execute Python scripts for data processing and visualization
        - Web development: Run JavaScript/TypeScript for frontend/backend development
        - System administration: Execute shell commands for environment setup
        - File processing: Read, transform, and write files programmatically
        - Educational coding: Provide safe environments for learning and experimentation
        - CI/CD workflows: Execute build scripts and deployment commands
        - API testing: Run code to test external services and APIs

        Usage with Strands Agent:
```python
        from strands import Agent
        from strands_tools.code_interpreter import AgentCoreCodeInterpreter

        # Create the code interpreter tool
        bedrock_agent_core_code_interpreter = AgentCoreCodeInterpreter(region="us-west-2")
        agent = Agent(tools=[bedrock_agent_core_code_interpreter.code_interpreter])

        # Create a session
        agent.tool.code_interpreter(
            code_interpreter_input={{
                "action": {{
                    "type": "initSession",
                    "description": "Data analysis session",
                    "session_name": "analysis-session"
                }}
            }}
        )

        # Execute Python code
        agent.tool.code_interpreter(
            code_interpreter_input={{
                "action": {{
                    "type": "executeCode",
                    "session_name": "analysis-session",
                    "code": "import pandas as pd\\ndf = pd.read_csv('data.csv')\\nprint(df.head())",
                    "language": "python"
                }}
            }}
        )

        # Write files to the sandbox
        agent.tool.code_interpreter(
            code_interpreter_input={{
                "action": {{
                    "type": "writeFiles",
                    "session_name": "analysis-session",
                    "content": [
                        {{"path": "config.json", "text": '{{"debug": true}}'}},
                        {{"path": "script.py", "text": "print('Hello, World!')"}}
                    ]
                }}
            }}
        )

        # Execute shell commands
        agent.tool.code_interpreter(
            code_interpreter_input={{
                "action": {{
                    "type": "executeCommand",
                    "session_name": "analysis-session",
                    "command": "ls -la && python script.py"
                }}
            }}
        )
```

        Args:
            code_interpreter_input: Structured input containing the action to perform.
                Must be a CodeInterpreterInput object with an 'action' field specifying
                the operation type and required parameters.

                Action Types and Required Fields:
                - InitSessionAction: type="initSession", description (required), session_name (optional)
                - ExecuteCodeAction: type="executeCode", session_name, code, language, clear_context (optional)
                  * language must be one of: {{supported_languages_enum}}
                - ExecuteCommandAction: type="executeCommand", session_name, command
                - ReadFilesAction: type="readFiles", session_name, paths (list)
                - WriteFilesAction: type="writeFiles", session_name, content (list of FileContent objects)
                - ListFilesAction: type="listFiles", session_name, path
                - RemoveFilesAction: type="removeFiles", session_name, paths (list)
                - ListLocalSessionsAction: type="listLocalSessions"

        Returns:
            Dict containing execution results in the format:
            {{
                "status": "success|error",
                "content": [{{"text": "...", "json": {{...}}}}]
            }}

            Success responses include:
            - Session information for session operations
            - Code execution output and results
            - File contents for read operations
            - Operation confirmations for write/delete operations

            Error responses include:
            - Session not found errors
            - Code compilation/execution errors
            - File system operation errors
            - Platform connectivity issues
        """

        auto_session_note = """**session_name is now optional in most operations**

        Sessions are automatically created when needed. You can now:
        • Omit session_name to use an auto-generated session (recommended for simple use cases)
        • Provide session_name for named sessions (useful for multi-step workflows)
        
        Quick example without session_name:
```python
        # No need to call initSession first
        agent.tool.code_interpreter(
            code_interpreter_input={{
                "action": {{
                    "type": "executeCode",
                    "code": "print('Hello, World!')",
                    "language": "python"
                }}
            }}
        )
```
        
        ---
        
        """

        # Set description: note + existing content
        self.code_interpreter.tool_spec["description"] = auto_session_note + description_template.format(
            supported_languages_list=", ".join([f"{lang.name}" for lang in self.get_supported_languages()])
        )

    @tool
    def code_interpreter(self, code_interpreter_input: CodeInterpreterInput) -> Dict[str, Any]:
        """Execute code in isolated sandbox environments."""

        if not self._started:
            self._start()

        if isinstance(code_interpreter_input, dict):
            logger.debug("Mapping dict to CodeInterpreterInput")
            action = CodeInterpreterInput.model_validate(code_interpreter_input).action
        else:
            action = code_interpreter_input.action

        logger.debug(f"Processing action: {type(action).__name__}")

        # Delegate to implementations
        if isinstance(action, InitSessionAction):
            return self.init_session(action)
        elif isinstance(action, ListLocalSessionsAction):
            return self.list_local_sessions()
        elif isinstance(action, ExecuteCodeAction):
            return self.execute_code(action)
        elif isinstance(action, ExecuteCommandAction):
            return self.execute_command(action)
        elif isinstance(action, ReadFilesAction):
            return self.read_files(action)
        elif isinstance(action, ListFilesAction):
            return self.list_files(action)
        elif isinstance(action, RemoveFilesAction):
            return self.remove_files(action)
        elif isinstance(action, WriteFilesAction):
            return self.write_files(action)
        else:
            return {"status": "error", "content": [{"text": f"Unknown action: {type(action)}"}]}

    def _start(self) -> None:
        """Start the platform."""
        if not self._started:
            self.start_platform()
            self._started = True
            logger.debug("Code Interpreter started")

    def _cleanup(self) -> None:
        """Clean up platform resources."""
        if self._started:
            self.cleanup_platform()
            self._started = False
            logger.debug("Code Interpreter cleaned up")

    def __del__(self):
        """Cleanup on destruction."""
        try:
            if self._started:
                logger.debug("Platform cleanup completed successfully")
                self._cleanup()
        except Exception as e:
            logger.debug("exception=<%s> | platform cleanup during destruction skipped", str(e))


def resolve_region(region_name: Optional[str] = None) -> str:
    """Resolve AWS region with fallback hierarchy (as strands-agents-tools resolved it)."""
    if region_name:
        return region_name

    try:
        session = boto3.Session()
        if session.region_name:
            return session.region_name
    except Exception:
        pass

    env_region = os.environ.get("AWS_REGION")
    if env_region:
        return env_region

    from strands.models.bedrock import DEFAULT_BEDROCK_REGION  # noqa: PLC0415

    return DEFAULT_BEDROCK_REGION


@dataclass
class SessionInfo:
    """One code interpreter session: the AWS session id, its description, and the client bound to it."""

    session_id: str  # AWS CI session ID
    description: str
    client: BedrockAgentCoreCodeInterpreterClient


class AgentCoreCodeInterpreter(CodeInterpreter):
    """``code_interpreter`` on Bedrock AgentCore sandboxes, with sessions created on demand by name.

    ``session_name=None`` gives this instance a random ``default_session`` (used when an action names
    none). Sessions are cached per instance (``_sessions``) and by name across instances
    (:data:`_session_mapping`), so a new instance reconnects to a still-READY session instead of
    creating one. ``persist_sessions=True`` (default) leaves sessions running when the instance goes
    away; they end at ``session_timeout_seconds``.
    """

    def __init__(
        self,
        region: Optional[str] = None,
        identifier: Optional[str] = None,
        session_name: Optional[str] = None,
        auto_create: bool = True,
        persist_sessions: bool = True,
        session_timeout_seconds: int = 900,
        boto_session: Optional[boto3.Session] = None,
    ) -> None:
        super().__init__()
        self.region = resolve_region(region)
        self.identifier = identifier or "aws.codeinterpreter.v1"
        self.auto_create = auto_create
        self.persist_sessions = persist_sessions
        self.session_timeout_seconds = session_timeout_seconds
        self.boto_session = boto_session

        if session_name is None:
            self.default_session = f"session-{uuid.uuid4().hex[:12]}"
        else:
            self.default_session = session_name

        self._sessions: Dict[str, SessionInfo] = {}

        logger.info(
            f"Initialized CodeInterpreter with session='{self.default_session}', "
            f"identifier='{self.identifier}', auto_create={auto_create}, "
            f"persist_sessions={persist_sessions}"
        )

    def start_platform(self) -> None:
        """Initialize the Bedrock AgentCore platform connection."""
        pass

    def cleanup_platform(self) -> None:
        """Clean up Bedrock AgentCore platform resources."""
        if not self._started:
            return

        # Only cleanup if persist_sessions=False
        if not self.persist_sessions:
            logger.info("Cleaning up Bedrock Agent Core platform resources")

            for session_name, session in list(self._sessions.items()):
                try:
                    session.client.stop()
                    logger.debug(f"Stopped session: {session_name}")
                except Exception as e:
                    logger.debug(f"Session {session_name} cleanup skipped: {e}")

            self._sessions.clear()
            logger.info("Bedrock AgentCore platform cleanup completed")
        else:
            logger.debug("Skipping cleanup - sessions persist (persist_sessions=True)")

    def init_session(self, action: InitSessionAction) -> Dict[str, Any]:
        """Start a new sandbox session named ``action.session_name`` on the configured identifier."""
        logger.info(f"Initializing Bedrock AgentCore sandbox session: {action.description}")

        session_name = action.session_name

        # Check if session already exists in instance cache
        if session_name in self._sessions:
            return {"status": "error", "content": [{"text": f"Session '{session_name}' already exists"}]}

        # Check if session name already in use (module-level cache)
        if session_name in _session_mapping:
            error_msg = (
                f"Session '{session_name}' is already in use by another instance. "
                f"Use a unique session name or reconnect to the existing session "
                f"via _ensure_session() instead of calling init_session() directly."
            )
            logger.error(error_msg)
            return {"status": "error", "content": [{"text": error_msg}]}

        try:
            # Create new sandbox client
            client = BedrockAgentCoreCodeInterpreterClient(region=self.region, session=self.boto_session)

            client.start(
                identifier=self.identifier,
                name=session_name,
                session_timeout_seconds=self.session_timeout_seconds,
            )

            aws_session_id = client.session_id

            # Store mapping in module-level cache
            _session_mapping[session_name] = aws_session_id

            # Store session info locally
            self._sessions[session_name] = SessionInfo(
                session_id=aws_session_id, description=action.description, client=client
            )

            logger.info(f"Initialized session: {session_name} (AWS ID: {aws_session_id})")

            response = {
                "status": "success",
                "content": [
                    {
                        "json": {
                            "sessionName": session_name,
                            "description": action.description,
                            "sessionId": aws_session_id,
                        }
                    }
                ],
            }

            return self._create_tool_result(response)

        except Exception as e:
            logger.error(f"Failed to initialize session '{session_name}': {str(e)}")
            return {
                "status": "error",
                "content": [{"text": f"Failed to initialize session '{session_name}': {str(e)}"}],
            }

    def list_local_sessions(self) -> Dict[str, Any]:
        """List all sessions created by this instance."""
        sessions_info = []
        for name, info in self._sessions.items():
            sessions_info.append(
                {
                    "sessionName": name,
                    "description": info.description,
                    "sessionId": info.session_id,
                }
            )

        return {
            "status": "success",
            "content": [{"json": {"sessions": sessions_info, "totalSessions": len(sessions_info)}}],
        }

    def _ensure_session(self, session_name: Optional[str]) -> tuple[str, Optional[Dict[str, Any]]]:
        """Make sure the session ``session_name`` (else ``default_session``) exists.

        Order: this instance's cache, then reconnect through :data:`_session_mapping` when that session
        is still READY, then (``auto_create``) start a new one. Returns ``(name, None)`` on success or
        ``(name, error_result)``; raises ``ValueError`` when it is missing and ``auto_create`` is off.
        """
        target_session = session_name if session_name else self.default_session

        logger.debug(f"Ensuring session: {target_session}")

        # Check local cache first
        if target_session in self._sessions:
            logger.debug(f"Using cached session: {target_session}")
            return target_session, None

        # Check module-level cache for AWS session ID
        aws_session_id = _session_mapping.get(target_session)

        if aws_session_id:
            # Found in module cache - try to reconnect
            logger.debug(f"Found session in module cache: {target_session} -> {aws_session_id}")

            try:
                client = BedrockAgentCoreCodeInterpreterClient(region=self.region, session=self.boto_session)

                # Verify session still exists and is ready
                session_info = client.get_session(interpreter_id=self.identifier, session_id=aws_session_id)

                if session_info.get("status") == "READY":
                    # Session is ready - reconnect to it
                    client.identifier = self.identifier
                    client.session_id = aws_session_id

                    self._sessions[target_session] = SessionInfo(
                        session_id=aws_session_id, description="Reconnected via module cache", client=client
                    )

                    logger.info(f"Reconnected to existing session: {target_session}")
                    return target_session, None
                else:
                    # Session exists but not ready - remove from cache
                    logger.warning(f"Session {target_session} not READY, removing from cache")
                    del _session_mapping[target_session]

            except Exception as e:
                # Session doesn't exist or error - remove from cache
                logger.debug(f"Session reconnection failed: {e}")
                if target_session in _session_mapping:
                    del _session_mapping[target_session]

        # Session not found - create new if auto_create enabled
        if self.auto_create:
            logger.info(f"Auto-creating session: {target_session}")

            init_action = InitSessionAction(
                type="initSession", session_name=target_session, description="Auto-initialized session"
            )
            result = self.init_session(init_action)

            if result.get("status") != "success":
                return target_session, result

            logger.info(f"Successfully auto-created session: {target_session}")
            return target_session, None

        # auto_create=False and session doesn't exist
        logger.debug(f"Session '{target_session}' not found (auto_create disabled)")
        raise ValueError(f"Session '{target_session}' not found. Create it first using initSession.")

    def execute_code(self, action: ExecuteCodeAction) -> Dict[str, Any]:
        """Execute code in a session (created on demand)."""
        session_name, error = self._ensure_session(action.session_name)
        if error:
            return error

        logger.debug(f"Executing {action.language} code in session '{session_name}'")

        params = {"code": action.code, "language": action.language.value, "clearContext": action.clear_context}
        response = self._sessions[session_name].client.invoke("executeCode", params)

        return self._create_tool_result(response)

    def execute_command(self, action: ExecuteCommandAction) -> Dict[str, Any]:
        """Execute a shell command in a session (created on demand)."""
        session_name, error = self._ensure_session(action.session_name)
        if error:
            return error

        logger.debug(f"Executing command in session '{session_name}'")

        params = {"command": action.command}
        response = self._sessions[session_name].client.invoke("executeCommand", params)

        return self._create_tool_result(response)

    def read_files(self, action: ReadFilesAction) -> Dict[str, Any]:
        """Read files from a session (created on demand)."""
        session_name, error = self._ensure_session(action.session_name)
        if error:
            return error

        logger.debug(f"Reading files from session '{session_name}'")

        params = {"paths": action.paths}
        response = self._sessions[session_name].client.invoke("readFiles", params)

        return self._create_tool_result(response)

    def list_files(self, action: ListFilesAction) -> Dict[str, Any]:
        """List a directory in a session (created on demand)."""
        session_name, error = self._ensure_session(action.session_name)
        if error:
            return error

        logger.debug(f"Listing files in session '{session_name}'")

        params = {"path": action.path}
        response = self._sessions[session_name].client.invoke("listFiles", params)

        return self._create_tool_result(response)

    def remove_files(self, action: RemoveFilesAction) -> Dict[str, Any]:
        """Remove files from a session (created on demand)."""
        session_name, error = self._ensure_session(action.session_name)
        if error:
            return error

        logger.debug(f"Removing files from session '{session_name}'")

        params = {"paths": action.paths}
        response = self._sessions[session_name].client.invoke("removeFiles", params)

        return self._create_tool_result(response)

    def write_files(self, action: WriteFilesAction) -> Dict[str, Any]:
        """Write files to a session (created on demand)."""
        session_name, error = self._ensure_session(action.session_name)
        if error:
            return error

        logger.debug(f"Writing {len(action.content)} files to session '{session_name}'")

        content_dicts = []
        for fc in action.content:
            if fc.blob is not None:
                content_dicts.append({"path": fc.path, "blob": fc.blob})
            else:
                content_dicts.append({"path": fc.path, "text": fc.text})
        params = {"content": content_dicts}
        response = self._sessions[session_name].client.invoke("writeFiles", params)

        return self._create_tool_result(response)

    def _create_tool_result(self, response) -> Dict[str, Any]:
        """Create tool result from response."""
        if "stream" in response:
            event_stream = response["stream"]
            for event in event_stream:
                if "result" in event:
                    result = event["result"]
                    is_error = response.get("isError", False)
                    return {
                        "status": "success" if not is_error else "error",
                        "content": [{"text": str(result.get("content"))}],
                    }

            return {"status": "error", "content": [{"text": f"Failed to create tool result: {str(response)}"}]}

        return response

    @staticmethod
    def get_supported_languages() -> List[LanguageType]:
        return [LanguageType.PYTHON, LanguageType.JAVASCRIPT, LanguageType.TYPESCRIPT]


__all__ = [
    "AgentCoreCodeInterpreter",
    "CodeInterpreter",
    "CodeInterpreterInput",
    "SessionInfo",
    "resolve_region",
]

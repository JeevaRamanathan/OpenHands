import re

from litellm.exceptions import ContextWindowExceededError

from agenthub.codeact_agent.prompt import (
    COMMAND_DOCS,
    EXAMPLES,
    GITHUB_MESSAGE,
    SYSTEM_PREFIX,
    SYSTEM_SUFFIX,
)
from opendevin.controller.agent import Agent
from opendevin.controller.state.state import State
from opendevin.core.config import config
from opendevin.core.exceptions import ContextWindowLimit
from opendevin.core.logger import opendevin_logger as logger
from opendevin.events.action import (
    Action,
    AgentFinishAction,
    AgentSummarizeAction,
    BrowseInteractiveAction,
    CmdRunAction,
    IPythonRunCellAction,
    MessageAction,
)
from opendevin.events.observation import (
    BrowserOutputObservation,
    CmdOutputObservation,
    IPythonRunCellObservation,
)
from opendevin.events.observation.summary import SummaryObservation
from opendevin.llm.llm import LLM
from opendevin.memory.condenser import MemoryCondenser
from opendevin.runtime.plugins import (
    AgentSkillsRequirement,
    JupyterRequirement,
    PluginRequirement,
)
from opendevin.runtime.tools import RuntimeTool

ENABLE_GITHUB = True


def parse_response(response) -> str:
    action = response.choices[0].message.content
    for lang in ['bash', 'ipython', 'browse']:
        if f'<execute_{lang}>' in action and f'</execute_{lang}>' not in action:
            action += f'</execute_{lang}>'
    return action


def action_to_str(action: Action) -> str:
    if isinstance(action, CmdRunAction):
        return f'{action.thought}\n<execute_bash>\n{action.command}\n</execute_bash>'
    elif isinstance(action, IPythonRunCellAction):
        return f'{action.thought}\n<execute_ipython>\n{action.code}\n</execute_ipython>'
    elif isinstance(action, BrowseInteractiveAction):
        return f'{action.thought}\n<execute_browse>\n{action.browser_actions}\n</execute_browse>'
    elif isinstance(action, MessageAction):
        return action.content
    return ''


def get_action_message(action: Action) -> dict[str, str] | None:
    if (
        isinstance(action, BrowseInteractiveAction)
        or isinstance(action, CmdRunAction)
        or isinstance(action, IPythonRunCellAction)
        or isinstance(action, MessageAction)
        or isinstance(action, AgentSummarizeAction)
    ):
        return {
            'role': 'user' if action.source == 'user' else 'assistant',
            'content': action_to_str(action),
        }
    return None


def get_observation_message(obs) -> dict[str, str] | None:
    if isinstance(obs, CmdOutputObservation):
        content = 'OBSERVATION:\n' + truncate_observation(obs.content)
        content += (
            f'\n[Command {obs.command_id} finished with exit code {obs.exit_code}]]'
        )
        return {'role': 'user', 'content': content}
    elif isinstance(obs, IPythonRunCellObservation):
        content = 'OBSERVATION:\n' + obs.content
        # replace base64 images with a placeholder
        splitted = content.split('\n')
        for i, line in enumerate(splitted):
            if '![image](data:image/png;base64,' in line:
                splitted[i] = (
                    '![image](data:image/png;base64, ...) already displayed to user'
                )
        content = '\n'.join(splitted)
        content = truncate_observation(content)
        return {'role': 'user', 'content': content}
    elif isinstance(obs, BrowserOutputObservation):
        content = 'OBSERVATION:\n' + truncate_observation(obs.content)
        return {'role': 'user', 'content': content}
    elif isinstance(obs, SummaryObservation):
        content = 'OBSERVATION:\n' + truncate_observation(obs.content)
        return {'role': 'user', 'content': obs.content}
    return None


def truncate_observation(observation: str, max_chars: int = 10000) -> str:
    """
    Truncate the middle of the observation if it is too long.
    """
    if len(observation) <= max_chars:
        return observation
    half = max_chars // 2
    return (
        observation[:half]
        + '\n[... Observation truncated due to length ...]\n'
        + observation[-half:]
    )


# FIXME: We can tweak these two settings to create MicroAgents specialized toward different area
def get_system_message() -> str:
    if ENABLE_GITHUB:
        return f'{SYSTEM_PREFIX}\n{GITHUB_MESSAGE}\n\n{COMMAND_DOCS}\n\n{SYSTEM_SUFFIX}'
    else:
        return f'{SYSTEM_PREFIX}\n\n{COMMAND_DOCS}\n\n{SYSTEM_SUFFIX}'


def get_in_context_example() -> str:
    return EXAMPLES


class CodeActAgent(Agent):
    VERSION = '1.5'
    """
    The Code Act Agent is a minimalist agent.
    The agent works by passing the model a list of action-observation pairs and prompting the model to take the next step.

    ### Overview

    This agent implements the CodeAct idea ([paper](https://arxiv.org/abs/2402.13463), [tweet](https://twitter.com/xingyaow_/status/1754556835703751087)) that consolidates LLM agents’ **act**ions into a unified **code** action space for both *simplicity* and *performance* (see paper for more details).

    The conceptual idea is illustrated below. At each turn, the agent can:

    1. **Converse**: Communicate with humans in natural language to ask for clarification, confirmation, etc.
    2. **CodeAct**: Choose to perform the task by executing code
    - Execute any valid Linux `bash` command
    - Execute any valid `Python` code with [an interactive Python interpreter](https://ipython.org/). This is simulated through `bash` command, see plugin system below for more details.

    ![image](https://github.com/OpenDevin/OpenDevin/assets/38853559/92b622e3-72ad-4a61-8f41-8c040b6d5fb3)

    ### Plugin System

    To make the CodeAct agent more powerful with only access to `bash` action space, CodeAct agent leverages OpenDevin's plugin system:
    - [Jupyter plugin](https://github.com/OpenDevin/OpenDevin/tree/main/opendevin/runtime/plugins/jupyter): for IPython execution via bash command
    - [SWE-agent tool plugin](https://github.com/OpenDevin/OpenDevin/tree/main/opendevin/runtime/plugins/swe_agent_commands): Powerful bash command line tools for software development tasks introduced by [swe-agent](https://github.com/princeton-nlp/swe-agent).
    ### Demo

    https://github.com/OpenDevin/OpenDevin/assets/38853559/f592a192-e86c-4f48-ad31-d69282d5f6ac

    *Example of CodeActAgent with `gpt-4-turbo-2024-04-09` performing a data science task (linear regression)*

    ### Work-in-progress & Next step

    [] Support web-browsing
    [] Complete the workflow for CodeAct agent to submit Github PRs

    """

    sandbox_plugins: list[PluginRequirement] = [
        # NOTE: AgentSkillsRequirement need to go before JupyterRequirement, since
        # AgentSkillsRequirement provides a lot of Python functions
        # and it need to be initialized before Jupyter for Jupyter to use those functions.
        AgentSkillsRequirement(),
        JupyterRequirement(),
    ]
    runtime_tools: list[RuntimeTool] = [RuntimeTool.BROWSER]
    jupyter_kernel_init_code: str = 'from agentskills import *'

    system_message: str = get_system_message()
    in_context_example: str = f"Here is an example of how you can interact with the environment for task solving:\n{get_in_context_example()}\n\nNOW, LET'S START!"

    def __init__(
        self,
        llm: LLM,
    ) -> None:
        """Initializes a new instance of the CodeActAgent class.

        Parameters:
        - llm (LLM): The llm to be used by this agent
        """
        super().__init__(llm)
        self.memory_condenser = (
            MemoryCondenser(
                llm
            )  # if config.agent.memory_condensation_enabled else None
        )
        self.reset()

    def reset(self) -> None:
        """
        Resets the CodeAct Agent.
        """
        super().reset()

    def step(self, state: State) -> Action:
        """
        Run the agent for one step.

        Parameters:
        - state: The current state of the environment.

        Returns:
        - CmdRunAction(command) - bash command to run
        - IPythonRunCellAction(code) - IPython code to run
        - BrowseInteractiveAction(browsergym_command) - BrowserGym commands to run
        - MessageAction(content) - Message action to run (e.g. ask for clarification)
        - AgentFinishAction() - end the interaction
        """
        logger.info(f'Running CodeActAgent v{self.VERSION}')

        messages: list[dict[str, str]] = self._get_messages(state)

        # FIXME move it out to LLM class
        # if the user has configured config.llm.max_input_tokens, we should start condensing when we hit it, to limit the costs
        if (
            config.llm.max_input_tokens is not None
            and self.memory_condenser is not None
            and self.memory_condenser.is_over_token_limit(messages)
        ):
            logger.info('Configured token limit exceeded. Condensing memory.')
            self._retry_with_condense(state)
            messages = self._get_messages(state)

        latest_user_message = next(
            (m for m in reversed(messages) if m['role'] == 'user'), None
        )
        if latest_user_message and latest_user_message['content'].strip() == '/exit':
            return AgentFinishAction()

        response = None
        # give it 3 tries
        attempt = 0
        while not response and attempt < 3:
            try:
                response = self.llm.completion(
                    messages=messages,
                    stop=[
                        '</execute_ipython>',
                        '</execute_bash>',
                        '</execute_browse>',
                    ],
                    temperature=0.0,
                )
            except ContextWindowExceededError:
                # FIXME move it to LLM class
                logger.warning('Context window exceeded. Condensing memory.')
                # Retry processing events with condensed memory
                if self.memory_condenser:
                    self._retry_with_condense(state)
                    messages = self._get_messages(state)

        action_str: str = parse_response(response)
        state.num_of_chars += sum(
            len(message['content']) for message in messages
        ) + len(action_str)

        if finish_command := re.search(r'<finish>.*</finish>', action_str, re.DOTALL):
            thought = action_str.replace(finish_command.group(0), '').strip()
            return AgentFinishAction(thought=thought)
        if bash_command := re.search(
            r'<execute_bash>(.*?)</execute_bash>', action_str, re.DOTALL
        ):
            # remove the command from the action string to get thought
            thought = action_str.replace(bash_command.group(0), '').strip()
            # a command was found
            command_group = bash_command.group(1).strip()

            if command_group.strip() == 'exit':
                return AgentFinishAction()
            return CmdRunAction(command=command_group, thought=thought)
        elif python_code := re.search(
            r'<execute_ipython>(.*?)</execute_ipython>', action_str, re.DOTALL
        ):
            # a code block was found
            code_group = python_code.group(1).strip()
            thought = action_str.replace(python_code.group(0), '').strip()
            return IPythonRunCellAction(
                code=code_group,
                thought=thought,
                kernel_init_code=self.jupyter_kernel_init_code,
            )
        elif browse_command := re.search(
            r'<execute_browse>(.*)</execute_browse>', action_str, re.DOTALL
        ):
            # BrowserGym actions was found
            browse_actions = browse_command.group(1).strip()
            thought = action_str.replace(browse_command.group(0), '').strip()
            return BrowseInteractiveAction(
                browser_actions=browse_actions, thought=thought
            )
        else:
            # We assume the LLM is GOOD enough that when it returns pure natural language
            # it wants to talk to the user
            return MessageAction(content=action_str, wait_for_response=True)

    def search_memory(self, query: str) -> list[str]:
        raise NotImplementedError('Implement this abstract method')

    def _get_messages(self, state: State) -> list[dict[str, str]]:
        messages = [
            {'role': 'system', 'content': self.system_message},
            {
                'role': 'user',
                'content': self.in_context_example,
            },
        ]

        for event in state.history:
            message = (
                get_action_message(event)
                if isinstance(event, Action)
                else get_observation_message(event)
            )
            if message:
                messages.append(message)

        latest_user_message = latest_user_message = next(
            (m for m in reversed(messages) if m['role'] == 'user'), None
        )
        if latest_user_message:
            if latest_user_message['content'].strip() == '/exit':
                return messages
            latest_user_message['content'] += (
                f'\n\nENVIRONMENT REMINDER: You have {state.max_iterations - state.iteration} turns left to complete the task.'
            )

        return messages

    def _retry_with_condense(self, state: State):
        events = state.history

        # FIXME retry?
        if self.memory_condenser:
            summary_action = self.memory_condenser.condense(events)

            if summary_action:
                state.history.replace_events_with_summary(summary_action)
                return

        # this is bad, we can't perform further LLM calls
        # raise an exception that we can use to set agent in ERROR state
        # and, perhaps, wipe the current task history?
        raise ContextWindowLimit(
            'Context window limit exceeded. Unable to condense memory.'
        )

from typing import List

from langchain.schema.language_model import BaseLanguageModel
from langchain.tools import BaseTool, Tool
from langchain.vectorstores import Chroma
from langchain_experimental.plan_and_execute.executors.base import ChainExecutor
from typing import Any, Dict, List, Optional

from langchain.callbacks.manager import (
    AsyncCallbackManagerForChainRun,
    CallbackManagerForChainRun,
)
from langchain.chains.base import Chain

from langchain_experimental.plan_and_execute.executors.base import BaseExecutor
from langchain_experimental.plan_and_execute.planners.base import BasePlanner
from langchain_experimental.plan_and_execute.schema import (
    BaseStepContainer,
    ListStepContainer,
)
from langchain_experimental.pydantic_v1 import Field
from langchain.agents.structured_chat.prompt import PREFIX, SUFFIX
from repopilot.agents.base import ChainExecutor, StructuredChatAgent
from repopilot.agents.agent_executor import AgentExecutor
from langchain.schema import Document

HUMAN_MESSAGE_TEMPLATE = """Previous steps: {previous_steps}

Current objective: {current_step}

{agent_scratchpad}"""

TASK_PREFIX = """Human request: {objective}

"""

ANALYZER_HUMAN_MESSAGE_TEMPLATE = """Current navigating objective: {objective}

Current notes from information agent: ##{current_notes}
## Agent scratchpad:o
{agent_scratchpad}
"""

FORMAT_INSTRUCTIONS = """Use a json blob to specify a tool by providing an action key (tool name) and an action_input key (tool input).

Valid "action" values: "Final Answer" or {tool_names}

Provide only ONE action per $JSON_BLOB, as shown:

```
{{{{
  "action": $TOOL_NAME,
  "action_input": $INPUT
}}}}
```

Follow this format:

Question: input question to answer
Thought: consider previous and subsequent steps, notes down some useful information (like code snippet) from observation
Action:
```
$JSON_BLOB
```
Observation: action result
... (repeat Thought/Action/Observation N times)
Thought: I know what to respond
Action:
```
{{{{
  "action": "Final Answer",
  "action_input": "Final response to human"
}}}}
```"""

def load_agent_analyzer(
    llm: BaseLanguageModel,
    prefix: str = "",
    suffix: str = "",
    vectorstore: Chroma = None,
    verbose: bool = False,
) -> ChainExecutor:
    """_summary_

    Args:
        llm (BaseLanguageModel): _description_
        prefix (str, optional): _description_. Defaults to ANALYZER_PREFIX.
        suffix (str, optional): _description_. Defaults to ANALYZER_SUFFIX.
        verbose (bool, optional): _description_. Defaults to False.

    Returns:
        ChainExecutor: _description_
    """
    input_variables = ["current_notes", "agent_scatchpad"]
    template = ANALYZER_HUMAN_MESSAGE_TEMPLATE
    
    def semantic_search(query):
        retrieved_docs = vectorstore.similarity_search(query, k=2)
        return [doc.page_content for doc in retrieved_docs]
    
    tools = [Tool(
            name="Relevant Information Search",
            func=semantic_search,
            description="useful to get the detailed code implementation of objects mentioned in the notes")]
    tools = []
    
    agent = StructuredChatAgent.from_llm_and_tools(
        llm, 
        tools=tools, 
        human_message_template=template, 
        input_variables=input_variables, 
        prefix=prefix, 
        suffix=suffix)
    
    agent_analyzer = AgentExecutor.from_agent_and_tools(
        agent=agent, 
        tools=tools,
        verbose=verbose, 
        return_intermediate_steps=False
    )
    # agent_analyzer.handle_parsing_errors = True
    return ChainExecutor(chain=agent_analyzer)

def load_agent_navigator(
    llm: BaseLanguageModel,
    tools: List[BaseTool],
    prefix: str = PREFIX,
    suffix: str = SUFFIX,
    verbose: int = 1,
    include_task_in_prompt: bool = False,
    save_trajectories_path: str = "./agent_trajectories",
    
) -> ChainExecutor:
    """
    Load an agent executor.

    Args:
        llm: BaseLanguageModel
        tools: List[BaseTool]
        verbose: bool. Defaults to False.
        include_task_in_prompt: bool. Defaults to False.

    Returns:
        ChainExecutor
    """
    input_variables = ["previous_steps", "current_step", "agent_scratchpad"]
    template = HUMAN_MESSAGE_TEMPLATE
    format_instructions = FORMAT_INSTRUCTIONS

    if include_task_in_prompt:
        input_variables.append("objective")
        template = TASK_PREFIX + template

    agent = StructuredChatAgent.from_llm_and_tools(
        llm,
        tools,
        human_message_template=template,
        input_variables=input_variables,
        prefix=prefix,
        suffix=suffix,
        format_instructions=format_instructions,
    )
    agent.save_trajectories_path = save_trajectories_path
    agent_executor = AgentExecutor.from_agent_and_tools(
        agent=agent, tools=tools, verbose=verbose, return_intermediate_steps=True 
    )
    agent_executor.handle_parsing_errors = True
    return ChainExecutor(chain=agent_executor)

class PlanSeeking(Chain):
    """Plan and execute a chain of steps."""

    planner: BasePlanner
    """The planner to use."""
    navigator: BaseExecutor
    """The executor to use."""
    analyzer: BaseExecutor
    step_container: BaseStepContainer = Field(default_factory=ListStepContainer)
    """The step container to use."""
    vectorstore: Chroma
    """The vectorstore-backed memory"""
    input_key: str = "input"
    output_key: str = "output"

    @property
    def input_keys(self) -> List[str]:
        return [self.input_key]

    @property
    def output_keys(self) -> List[str]:
        return [self.output_key]

    def _call(
        self,
        inputs: Dict[str, Any],
        run_manager: Optional[CallbackManagerForChainRun] = None,
    ) -> Dict[str, Any]:
        verbose_langchain = True if self.verbose > 0 else False
        current_notes = ""
        plan = self.planner.plan(
            inputs,
            callbacks=run_manager.get_child() if run_manager else None,
        )
        if run_manager:
            plan_str = ""
            for i, step in enumerate(plan.steps):
                plan_str += f"Step {i}: {step.value}\n"
            run_manager.on_text(plan_str, verbose=verbose_langchain)
        for i, step in enumerate(plan.steps):
            _new_inputs = {
                "previous_steps": self.step_container,
                "current_step": step,
                "objective": inputs[self.input_key],
            }
            new_inputs = {**_new_inputs, **inputs}          
            response, intermediate_steps = self.navigator.step(
                new_inputs,
                callbacks=run_manager.get_child() if run_manager else None,
            )
            for j, react_step in enumerate(intermediate_steps):
                if isinstance(react_step[1], list):
                    obs_strings = [str(x) for x in react_step[1]]
                    tool_output = "\n".join(obs_strings)
                else:
                    tool_output = str(react_step[1])
                current_notes += f"\n\nStep: {step.value}\n\nSubstep:{j}\n\Thought: {react_step[0].log.split('Action:')[0]}\nOutput: {tool_output}\n\n"
                vec_note = react_step[0].log + "\n" + tool_output
                self.vectorstore.add_documents([Document(page_content=vec_note)])
            early_exit = "Final Answer" in response.response or "final answer" in response.response or "Solved" in response.response or "solved" in response.response
            current_notes += f"Response for step {i}: {response.response}"
            self.step_container.add_step(step, response)
            if early_exit:
                break
                
        ## Run the analyzer
        analyzer_inputs = {
            "current_notes": current_notes,
            "objective": inputs[self.input_key],
        }
        answer = self.analyzer.step(
            analyzer_inputs,
            callbacks=run_manager.get_child() if run_manager else None,
        )
        return {self.output_key: answer}

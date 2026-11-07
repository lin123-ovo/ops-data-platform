"""Opt-in routing regression: real model decisions, without executing search tools.

Run with RUN_LIVE_AGENT_ROUTING_TESTS=1 python -m unittest
tests.test_agent_routing_live -v. Uses the configured model and incurs model usage.
"""
from __future__ import annotations

import os
import unittest

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.prompts import RAG_AGENT_SYSTEM_PROMPT
from app.agent.tools import get_agent_tools
from app.core.llm import get_llm
from app.services.chat_service import ChatService


@unittest.skipUnless(os.environ.get('RUN_LIVE_AGENT_ROUTING_TESTS') == '1', 'Live model routing is opt-in')
class LiveAgentRoutingTests(unittest.TestCase):
    def decision(self, question: str, *, streaming: bool = False, history: list | None = None) -> AIMessage:
        model = get_llm(streaming=streaming).bind_tools(get_agent_tools())
        inputs = ChatService()._build_agent_input(question)['messages']
        messages = [SystemMessage(content=RAG_AGENT_SYSTEM_PROMPT)]
        messages.append(SystemMessage(content=inputs[0]['content']))
        messages.extend(history or [])
        messages.append(HumanMessage(content=question))
        return model.invoke(messages)

    def test_explicit_hong_kong_weather_uses_web_in_both_model_modes(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                response = self.decision('今天香港天气怎么样', streaming=streaming)
                web_calls = [call for call in response.tool_calls if call['name'] == 'search_web']
                self.assertTrue(web_calls, response.content)
                self.assertTrue(any('香港' in call['args']['query'] or 'Hong Kong' in call['args']['query'] for call in web_calls))

    def test_missing_location_clarifies_without_search(self):
        response = self.decision('今天天气怎么样')
        self.assertFalse(response.tool_calls)
        self.assertTrue(response.text.strip())

    def test_city_in_followup_uses_web(self):
        response = self.decision('香港', history=[
            HumanMessage(content='今天天气怎么样'),
            AIMessage(content='你想查询哪个城市的天气？'),
        ])
        self.assertIn('search_web', [call['name'] for call in response.tool_calls])

    def test_greeting_does_not_search(self):
        self.assertFalse(self.decision('你好').tool_calls)

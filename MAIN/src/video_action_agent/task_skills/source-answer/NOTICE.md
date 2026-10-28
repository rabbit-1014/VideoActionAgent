# LlamaIndex 来源问答提示适配归属

Source: https://github.com/run-llama/llama_index/blob/962940ddc079cc21701d28d1237c84c82a7c5164/llama-index-core/llama_index/core/prompts/default_prompts.py
Commit: 962940ddc079cc21701d28d1237c84c82a7c5164
Adapted path: src/video_action_agent/task_skills/source-answer/SKILL.md (v1)
Changes: retain the source-only principle of DEFAULT_TEXT_QA_PROMPT_TMPL ("Given the context information and not prior knowledge, answer the query"); adapt the free-text answer template to an evidence-bound JSON contract with three fixed exits (answered/partial/not_in_source), citation-derived times, and explicit missing/unverified lists. No LlamaIndex installation or runtime use. This is a prompt-method adaptation, not deployment of LlamaIndex or proof of answer quality.

Full upstream license follows:
The MIT License

Copyright (c) Jerry Liu

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

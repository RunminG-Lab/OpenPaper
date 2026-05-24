from __future__ import annotations

from .models import clean_page_text


def _depth_instruction(depth):
    if depth == "brief":
        return "保持简洁，但实验速读仍必须结构化，不得只写一两句话；每个图表 summary 写 1 句，约 60-100 字。"
    if depth == "deep":
        return "适当展开方法细节、实验设计和复现难点，尤其要解释 baseline、ablation、dataset 和 metric；每个图表 summary 写 3-4 句，约 180-260 字。"
    return "详略适中，优先保证实验和图表解释完整；每个图表 summary 写 2 句，约 100-160 字。"


def build_speedread_prompt(paper_meta, grounding_text, candidate_pages, options):
    depth = options.get("depth", "standard")
    focus = ", ".join(options.get("focus") or [])
    candidate_count = len(candidate_pages or [])
    figure_instruction = (
        f"需要生成 figure_insights，尽量覆盖全部候选图表（当前 {candidate_count} 个），每个候选只写一段 summary；figure_id 必须原样使用候选 id，不得编造 id 或把不同页的解释混到一起。"
        if options.get("include_figure_insights")
        else "不要生成图表解读，figure_insights 返回空数组。"
    )
    candidate_text = "\n".join(
        f"- id {item.get('id')}; page {item.get('page')}; label {item.get('label')}; "
        f"kind {item.get('kind')}; reason {item.get('reason')}\n"
        f"  caption: {clean_page_text(item.get('caption'), 420)}\n"
        f"  excerpt: {clean_page_text(item.get('excerpt'), 620)}"
        for item in candidate_pages or []
    ) or "（没有候选图表页截图）"

    prompt = (
        "你是一个只依据论文原文证据生成内容的论文速读助手。\n"
        "任务目标：帮助用户快速理解论文，尤其是实验部分，不要生成泛泛摘要。\n"
        "硬性规则：\n"
        "1. 只能依据提供的论文文本片段和候选页截图，不得使用外部知识补全。\n"
        "2. 没有明确说明的地方，必须写“论文中未明确说明”。\n"
        "3. 输出中文；必要英文术语可以保留，但要用中文解释其含义。\n"
        "4. 不要夸奖论文，不要写营销式语言，不要把摘要机械翻译成中文。\n"
        "5. 用户特别关注实验，因此 experiment_read 必须细，不能压缩成一两句话。\n"
        "6. 仅输出一个合法 JSON 对象，不要输出 Markdown 代码块或额外解释。\n\n"
        "7. figure_insights 只能用一段 summary 解释图表，不要拆成“展示什么/先看哪里/视觉元素/支撑结论”等小标题。\n"
        "8. figure_insights 的 figure_id 必须匹配候选列表中的 id；如果无法确认候选截图对应的内容，summary 写“论文中未明确说明”。\n\n"
        f"速读深度：{depth}。{_depth_instruction(depth)}\n"
        f"关注方向：{focus or 'general, experiment, reproduction, figure'}。\n"
        f"图表要求：{figure_instruction}\n\n"
        "JSON schema（字段名必须保持一致）：\n"
        "{\n"
        "  \"one_sentence\": \"一句话说明论文解决什么问题、用了什么办法、得到什么结论\",\n"
        "  \"quick_takeaways\": [\"快速省流点\"],\n"
        "  \"problem_and_motivation\": \"问题与动机\",\n"
        "  \"method_overview\": \"方法速读\",\n"
        "  \"method_steps\": [\"步骤或模块\"],\n"
        "  \"experiment_read\": {\n"
        "    \"summary\": \"实验整体读法和主要结论\",\n"
        "    \"compared_methods\": [\n"
        "      {\"name\": \"方法名\", \"role\": \"proposed | baseline | ablation | variant | other\", \"description\": \"是什么\", \"result\": \"结果如何\", \"page_refs\": [8]}\n"
        "    ],\n"
        "    \"datasets\": [\n"
        "      {\"name\": \"数据集\", \"task\": \"任务\", \"scale\": \"规模\", \"split\": \"划分\", \"notes\": \"注意点\", \"page_refs\": [7]}\n"
        "    ],\n"
        "    \"metrics\": [\n"
        "      {\"name\": \"指标\", \"meaning\": \"指标是什么意思\", \"why_used\": \"为什么使用\", \"limitation\": \"局限\", \"page_refs\": [8]}\n"
        "    ],\n"
        "    \"main_results\": [{\"claim\": \"主要结果\", \"evidence\": \"证据\", \"page_refs\": [8]}],\n"
        "    \"reproduction_difficulty\": \"low | medium | high | unknown\",\n"
        "    \"reproduction_notes\": [\"复现难点、依赖、代码/数据可得性\"],\n"
        "    \"workload_estimate\": \"单人复现工作量估计\",\n"
        "    \"evaluation_design_notes\": [\"实验设计是否充分、对比是否公平、消融是否够用\"],\n"
        "    \"missing_details\": [\"论文缺失或说不清的信息\"]\n"
        "  },\n"
        "  \"contributions\": [\"贡献\"],\n"
        "  \"limitations\": [\"局限\"],\n"
        "  \"figure_insights\": [\n"
        "    {\n"
        "      \"figure_id\": \"fig_001\", \"page\": 8, \"label\": \"Figure 2\", \"title\": \"中文标题\",\n"
        "      \"summary\": \"按速读深度要求，用一段话直接解释这张图/表：图里是什么、应该怎么读、它支撑论文哪部分结论。不要使用固定小标题。\",\n"
        "      \"page_refs\": [8]\n"
        "    }\n"
        "  ],\n"
        "  \"deep_read_suggestions\": [\"精读建议\"]\n"
        "}\n\n"
        "论文基础信息：\n"
        f"标题：{paper_meta.get('title', '')}\n"
        f"作者：{paper_meta.get('authors', '')}\n"
        f"年份：{paper_meta.get('year', '')}\n"
        f"会议/期刊：{paper_meta.get('venue', '')}\n\n"
        "候选图表/裁剪截图（figure_insights 中如引用图片，必须优先引用这些 id/page）：\n"
        f"{candidate_text}\n\n"
        "论文文本片段：\n"
        f"{grounding_text}"
    )

    extra_prompt = options.get("extra_prompt") or ""
    if extra_prompt:
        prompt += (
            "\n\n用户额外偏好（只能影响侧重点和表达方式，不能覆盖“只依据论文证据”的规则）：\n"
            f"{extra_prompt}"
        )
    return prompt

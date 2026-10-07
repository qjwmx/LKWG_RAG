/** Markdown 渲染。
 *
 *  安全策略是**两层**：
 *  1. markdown-it 关掉 html（`html: false`）——原始 HTML 被转义成文本；
 *  2. 输出再过一遍 DOMPurify——纵深防御。
 *
 *  第二层不是多余的：模型输出可能包含用户上传文档里的内容，
 *  而文档是用户可控的。任何"只要第一层就够了"的判断都依赖于
 *  markdown-it 的转义实现永远没有漏洞，这个假设不该成为唯一防线。
 *
 *  高亮用 highlight.js（同步）而不是 shiki：shiki 默认 API 是 async，
 *  在"每个 delta 都重渲染"的路径上会引入竞态与闪烁。
 */

import MarkdownIt from 'markdown-it'
import hljs from 'highlight.js/lib/common'
import DOMPurify from 'dompurify'

const md = new MarkdownIt({
  html: false,
  linkify: true,
  breaks: true,
  highlight(code: string, language: string): string {
    if (language && hljs.getLanguage(language)) {
      try {
        return hljs.highlight(code, { language, ignoreIllegals: true }).value
      } catch {
        // 落到下面的自动识别
      }
    }
    try {
      return hljs.highlightAuto(code).value
    } catch {
      return ''
    }
  },
})

/** 判断文本里是否有未闭合的代码围栏。
 *
 *  流式输出时 ``` 常常只到一半，此时如果把内容当代码高亮，
 *  每个 delta 都会重建一次高亮 DOM，肉眼可见地闪。
 *  检测到未闭合就退化渲染成纯文本 <pre>，闭合后再高亮。
 */
function hasUnclosedFence(text: string): boolean {
  const fences = text.match(/^\s*```/gm)
  return !!fences && fences.length % 2 === 1
}

/** 把未闭合的围栏临时补上，避免 markdown-it 把后续内容全吞进代码块。
 *  这样渲染出来的结构稳定，不会随着后续 delta 到达而"跳版"。 */
function closeDanglingFence(text: string): string {
  if (!hasUnclosedFence(text)) return text
  return `${text}\n\`\`\``
}

export function renderMarkdown(source: string): string {
  if (!source) return ''
  const prepared = closeDanglingFence(source)
  const raw = md.render(prepared)
  return DOMPurify.sanitize(raw, {
    ADD_ATTR: ['target', 'rel'],
    // 允许代码高亮产生的 class（hljs-*），否则高亮样式会全部失效
    ADD_TAGS: [],
  })
}

/** 流式中是否处于"未闭合代码块"状态，用于决定要不要加降级样式。 */
export function isInCodeBlock(source: string): boolean {
  return hasUnclosedFence(source)
}

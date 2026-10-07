import { nextTick, onBeforeUnmount, ref, watch, type Ref } from 'vue'

/** 消息列表的自动滚动。
 *
 *  关键行为：**用户手动往上翻时暂停自动滚动**。
 *  不加这个判断的话，流式回答会把正在读历史消息的用户不断拽回底部，
 *  这是聊天界面最常见的体验问题。
 */
export function useAutoScroll(container: Ref<HTMLElement | null>, trigger: Ref<unknown>) {
  const pinned = ref(true)
  const showJumpButton = ref(false)

  function isNearBottom(): boolean {
    const el = container.value
    if (!el) return true
    // 60px 容差：不是精确贴底才算"在底部"，否则轻微像素差就会误判
    return el.scrollHeight - el.scrollTop - el.clientHeight < 60
  }

  function onScroll(): void {
    const near = isNearBottom()
    pinned.value = near
    showJumpButton.value = !near
  }

  function scrollToBottom(smooth = false): void {
    const el = container.value
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: smooth ? 'smooth' : 'auto' })
    pinned.value = true
    showJumpButton.value = false
  }

  watch(trigger, async () => {
    if (!pinned.value) return
    await nextTick()
    scrollToBottom()
  })

  onBeforeUnmount(() => {
    container.value?.removeEventListener('scroll', onScroll)
  })

  function attach(): void {
    container.value?.addEventListener('scroll', onScroll, { passive: true })
  }

  return { pinned, showJumpButton, scrollToBottom, attach }
}

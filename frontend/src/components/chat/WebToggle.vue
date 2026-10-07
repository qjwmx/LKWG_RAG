<script setup lang="ts">
import { computed } from 'vue'

/**
 * 「联网搜索」高亮开关。
 *
 * 交互契约（用户明确要求）：
 * - **点一下高亮 = 本轮会联网**；不亮 = 不联网。
 * - 状态必须**一眼可辨**：靠底色 + 文字色 + 图标三者同时变化，
 *   而不是只改一个细边框——只改边框的话，在壁纸与毛玻璃上几乎看不出来。
 *
 * 为什么不用 daisyUI 的 ``toggle`` 复选框（原先用的就是它）：
 * 复选框表达的是"某个配置项的值"，而这里要表达的是
 * **"这一轮要不要真的去调外部接口"**——是一个动作意图，而且按次计费。
 * 按钮 + 高亮更贴近这个语义，也更符合用户的要求。
 *
 * 对话模式与攻略模式共用这一个组件，避免两处的文案与状态慢慢跑偏。
 */
const props = defineProps<{
  /** 当前是否开启（v-model） */
  modelValue: boolean
  /** 服务端是否真的可用（配了 TAVILY_API_KEY 且没被禁用） */
  available: boolean
  /** 是否禁用（例如正在生成中） */
  disabled?: boolean
  /** 紧凑模式：只显示图标与短文案 */
  compact?: boolean
}>()

const emit = defineEmits<{ (e: 'update:modelValue', value: boolean): void }>()

function toggle(): void {
  if (!props.available || props.disabled) return
  emit('update:modelValue', !props.modelValue)
}

/** 不可用时给出**具体原因**，而不是一句"不可用"。
 *  用户看到"未配置"就知道该去填 key，而不是反复点。 */
const title = computed(() => {
  if (!props.available) return '服务端未配置联网检索（缺少 TAVILY_API_KEY），当前只能用本地知识库。'
  if (props.disabled) return '正在生成中，稍后可切换。'
  return props.modelValue
    ? '已开启：本轮会联网检索，结果会标注「网络来源（未经验证）」。点一下关闭。'
    : '未开启：只用本地知识库。点一下开启联网检索。'
})
</script>

<template>
  <button
    type="button"
    class="web-toggle"
    :class="[
      modelValue && available ? 'web-toggle-on' : 'web-toggle-off',
      !available ? 'web-toggle-disabled' : '',
    ]"
    :disabled="!available || disabled"
    :aria-pressed="modelValue && available"
    :title="title"
    @click="toggle"
  >
    <!-- 地球图标。开启时是实心亮色，关闭时是描边暗色——
         形状一致、明暗相反，比换图标更容易看出"同一个开关的两种状态"。 -->
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      :fill="modelValue && available ? 'currentColor' : 'none'"
      stroke="currentColor"
      :stroke-width="modelValue && available ? 0 : 1.8"
      class="h-3.5 w-3.5 shrink-0"
      aria-hidden="true"
    >
      <path
        d="M12 2a10 10 0 100 20 10 10 0 000-20zm0 0c2.5 2.5 4 6 4 10s-1.5 7.5-4 10m0-20C9.5 4.5 8 8 8 12s1.5 7.5 4 10M2.5 9h19M2.5 15h19"
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linecap="round"
      />
    </svg>

    <span class="whitespace-nowrap">{{ compact ? '联网' : '联网搜索' }}</span>

    <!-- 不可用时明确写出来，别让用户以为是开关坏了 -->
    <span v-if="!available" class="opacity-70">（未配置）</span>
    <!-- 开启时给一个明确的"已开"字样：只靠颜色对色觉障碍用户不友好 -->
    <span v-else-if="modelValue" class="web-toggle-badge">已开</span>
  </button>
</template>

<style scoped>
/* 用 scoped 而不是全局 @utility：这个组件的样式不外泄，
   而它的高亮态要覆盖 daisyUI 的按钮默认样式，放 scoped 里最省事。

   ★ 底色必须够实：它坐在壁纸/毛玻璃上，半透明浅底会让
   "高亮"与"不亮"两种状态的对比度都掉到看不出差别。 */
.web-toggle {
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  border-radius: 9999px;
  padding: 0.2rem 0.55rem;
  font-size: 11px;
  line-height: 1.4;
  border: 1px solid transparent;
  transition:
    background-color 0.15s ease,
    color 0.15s ease,
    border-color 0.15s ease,
    box-shadow 0.15s ease;
}

/* 关闭态：低调的描边，明确"当前不联网" */
.web-toggle-off {
  background-color: color-mix(in oklab, var(--color-base-100) 70%, transparent);
  border-color: var(--hairline);
  color: color-mix(in oklab, var(--color-base-content) 62%, transparent);
}
.web-toggle-off:hover:not(:disabled) {
  background-color: color-mix(in oklab, var(--color-base-100) 85%, transparent);
  color: var(--color-base-content);
}

/* 开启态：**高亮**。主色实底 + 主色描边 + 柔和外发光，
   三重信号叠加，在任何壁纸上都能一眼看出"现在会联网"。

   ★ 底色**不能**用 ``color-mix(primary 88%, base-100)`` 去"调浅"。
   探针实测：混入 base-100 后底色变亮，而文字仍是 primary-content，
   对比度从达标掉到 **4.12:1**（需 4.5）。
   ``--color-primary`` + ``--color-primary-content`` 是 daisyUI 自己
   保证过的配对，直接用它就对了——不要自作聪明去调。 */
.web-toggle-on {
  background-color: var(--color-primary);
  border-color: var(--color-primary);
  color: var(--color-primary-content);
  font-weight: 600;
  box-shadow: 0 0 0 2px color-mix(in oklab, var(--color-primary) 22%, transparent);
}

/* 不可用：压到最低存在感，但仍可读出"未配置" */
.web-toggle-disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

/* 「已开」小徽标。
 *
 * ★ 这里刻意**不用自己的半透明底色**。原先写的是
 * ``color-mix(primary-content 22%, transparent)`` 压在 primary 实底上——
 * 那会把底色提亮，而文字还是 primary-content，探针实测只有 **2.89:1**
 * （连大字的 3.0 都不到）。
 * 现在只用字重与字号做区分，颜色与主标签完全一致，对比度随主标签达标。 */
.web-toggle-badge {
  font-size: 10px;
  font-weight: 500;
  letter-spacing: 0.02em;
  /* 用与主标签同一个颜色，避免引入新的对比度风险 */
  color: inherit;
  /* 只加一圈描边做视觉分隔。**不能用半透明填充**——那会提亮底色、
     把文字对比度压下去（实测只有 2.89:1）。描边不改变底色。 */
  border: 1px solid currentColor;
  border-radius: 9999px;
  padding: 0 0.3rem;
  opacity: 0.9;
}
</style>

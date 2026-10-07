<script setup lang="ts">
import { computed, ref } from 'vue'
import { useWallpaper, OPACITY_MAX, OPACITY_MIN, UPLOAD_MAX_MB } from '@/composables/useWallpaper'
import { useTheme } from '@/composables/useTheme'
import {
  CUSTOM_ID,
  NONE_ID,
  WALLPAPER_PRESETS,
  findPreset,
  thumbCss,
} from '@/composables/wallpapers'

/** 壁纸选择器。
 *
 *  用 daisyUI dropdown（`details` 形式）而不是 modal：
 *  换壁纸是"边看边调"的操作，弹窗会挡住整个页面，
 *  而 dropdown 只有面板那么大，改一下就能看到背景变化。
 *
 *  `details` 形式的好处是**不需要 JS 管理开合**（原生语义 + 键盘可用），
 *  代价是点击外部不会自动关闭——所以下面挂了一个透明遮罩来补这个行为。
 */

const {
  source,
  presetId,
  opacity,
  customUrl,
  customName,
  customError,
  active,
  choosePreset,
  uploadCustom,
  clearCustom,
  setOpacity,
  disable,
} = useWallpaper()

const { theme } = useTheme()

/** 面板开合由 `<details>` 原生管理（语义 + 键盘可用，不需要 JS）。
 *  `open` 只是镜像状态，用来决定"点击外部关闭"的遮罩要不要渲染。 */
const open = ref(false)
const root = ref<HTMLDetailsElement | null>(null)
const uploading = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)

/** 是否已经为"首次上传"自动抬高过浓度。
 *  只抬一次：之后用户自己调的值不该被反复覆盖。 */
const autoRaised = ref(false)

/** 当前选中项的 id：none / preset id / custom */
const selectedId = computed(() => {
  if (source.value === 'none') return NONE_ID
  if (source.value === 'custom') return CUSTOM_ID
  return presetId.value
})

const currentLabel = computed(() => {
  if (source.value === 'none') return '无壁纸'
  if (source.value === 'custom') return customName.value || '自定义图片'
  return findPreset(presetId.value)?.label || '壁纸'
})

/** 缩略图按当前主题取色，否则浅色主题下会看到一排深色块。 */
function thumb(id: string): string {
  const preset = findPreset(id)
  return preset ? thumbCss(preset, theme.value) : 'transparent'
}

async function onFile(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  uploading.value = true
  try {
    const ok = await uploadCustom(file)
    if (ok) onUploaded()
  } finally {
    uploading.value = false
    // 清空 value：否则连续选同一个文件不会再触发 change
    input.value = ''
  }
}

/** 上传成功后自动把浓度调到较高的档。
 *
 *  用户上传照片的用意就是"想看见它"，而默认 35% 在玻璃后面非常淡
 *  （亮照片合成后与纯底色色差只有约 8）——很容易被当成"没上传成功"。
 *  首次上传时抬到 50%，让效果立刻可见；之后不再自动改，
 *  免得用户调好的浓度被反复覆盖。 */
function onUploaded(): void {
  if (source.value === 'custom' && !autoRaised.value) {
    autoRaised.value = true
    if (opacity.value < 0.5) setOpacity(0.5)
  }
}

function close(): void {
  // 只改原生属性，不动 open：让 <details> 保持唯一事实来源。
  // 反过来做（既设 :open 又听 toggle）会形成回环，
  // 某些浏览器下表现为"点了关不掉"。
  if (root.value) root.value.open = false
  open.value = false
}
</script>

<template>
  <details
    ref="root"
    class="dropdown dropdown-end"
    @toggle="open = ($event.target as HTMLDetailsElement).open"
  >
    <summary
      class="btn btn-ghost btn-sm btn-square list-none"
      title="壁纸"
      aria-label="壁纸"
    >
      <!-- 图标比原来大一档（h-4 → h-5）：顶栏里它和主题切换按钮并排，
           两个图标大小不一致会显得参差。 -->
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-5 w-5">
        <path d="M4 4h16a1 1 0 011 1v14a1 1 0 01-1 1H4a1 1 0 01-1-1V5a1 1 0 011-1zm1 2v9.6l3.4-3.4a1 1 0 011.4 0L14 16.2l1.8-1.8a1 1 0 011.4 0L19 16.2V6H5zm3 2.5a1.5 1.5 0 110 3 1.5 1.5 0 010-3z" />
      </svg>
    </summary>

    <!-- 点击外部关闭。details 原生不支持，用一个透明全屏遮罩补上；
         z-40 低于面板的 z-50，所以不会挡住面板自身的点击。 -->
    <div
      v-if="open"
      class="fixed inset-0 z-40 cursor-default"
      aria-hidden="true"
      @click="close"
    />

    <!-- ★ 必须带 `dropdown-content`。
         daisyUI 靠 `.dropdown:not(details, .dropdown-open, …) .dropdown-content`
         这条选择器把面板藏起来——类名少一个，这条规则就不匹配，
         面板会**永远显示**（表现为"一进页面就有一块浮层挂在右上角"）。
         这不是样式问题，是组件契约问题，所以放在最前面。 -->
    <div
      class="dropdown-content glass-panel rounded-box z-50 mt-1 w-[19rem] p-3 shadow-lg"
      role="dialog"
      aria-label="壁纸设置"
    >
      <div class="mb-2 flex items-center justify-between">
        <span class="text-xs font-semibold">壁纸</span>
        <span class="ink-subtle text-[10px]">{{ currentLabel }}</span>
      </div>

      <!-- 缩略图网格：3 列，每格是壁纸的真实渐变（不是示意色块） -->
      <div class="grid grid-cols-3 gap-2">
        <!-- 无壁纸：回到纯主题底色。底部也有「关闭壁纸」按钮，
             但这里保留一格——壁纸选择器里"能选回没有"是标准做法。 -->
        <button
          type="button"
          class="relative aspect-video overflow-hidden rounded-md border transition-colors"
          :class="
            selectedId === NONE_ID
              ? 'border-primary'
              : 'border-base-content/15 hover:border-base-content/35'
          "
          title="无壁纸（纯主题底色）"
          @click="choosePreset(NONE_ID)"
        >
          <span class="bg-base-100 absolute inset-0" />
          <span class="bg-base-content/20 absolute inset-0 flex items-center justify-center">
            <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor" class="h-4 w-4">
              <path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z" />
            </svg>
          </span>
          <span
            v-if="selectedId === NONE_ID"
            class="border-primary absolute inset-0 rounded-md border-2"
          />
        </button>

        <!-- 自定义图片：**上传后直接显示缩略图**。
             没有这一格的话，上传成功后面板上只有一行小字变了个文件名，
             用户无法确认到底生效没有——这正是"看起来没替换成功"的来源之一。 -->
        <button
          v-if="customUrl"
          type="button"
          class="relative aspect-video overflow-hidden rounded-md border transition-colors"
          :class="
            selectedId === CUSTOM_ID
              ? 'border-primary'
              : 'border-base-content/15 hover:border-base-content/35'
          "
          title="自定义图片"
          @click="choosePreset(CUSTOM_ID)"
        >
          <img :src="customUrl" alt="自定义壁纸" class="h-full w-full object-cover" />
          <span
            v-if="selectedId === CUSTOM_ID"
            class="border-primary absolute inset-0 rounded-md border-2"
          />
          <span class="absolute inset-x-0 bottom-0 truncate bg-black/75 px-1 py-0.5 text-[9px] text-white">
            自定义
          </span>
        </button>

        <!-- 预设 -->
        <button
          v-for="preset in WALLPAPER_PRESETS"
          :key="preset.id"
          type="button"
          class="relative aspect-video overflow-hidden rounded-md border transition-colors"
          :class="
            selectedId === preset.id
              ? 'border-primary'
              : 'border-base-content/15 hover:border-base-content/35'
          "
          :title="preset.label"
          :style="{ backgroundImage: thumb(preset.id) }"
          @click="choosePreset(preset.id)"
        >
          <span
            v-if="selectedId === preset.id"
            class="border-primary absolute inset-0 rounded-md border-2"
          />
          <!-- 缩略图上的名称条：原来用 bg-black/45 + 白字，实测对比度只有
               3.4:1（黑底 45% 太透，壁纸一浅就压不住白字）。
               改成更实的不透明黑，保证任何缩略图上都读得清。 -->
          <span
            class="absolute inset-x-0 bottom-0 truncate bg-black/75 px-1 py-0.5 text-[9px] text-white"
          >
            {{ preset.label }}
          </span>
        </button>
      </div>

      <div class="divider my-2.5 text-[10px]">自定义</div>

      <!-- 上传自定义图片 -->
      <div class="flex items-center gap-2">
        <input
          ref="fileInput"
          type="file"
          accept="image/*"
          class="file-input file-input-xs flex-1"
          :disabled="uploading"
          @change="onFile"
        />
        <button
          v-if="source === 'custom'"
          type="button"
          class="btn btn-ghost btn-xs"
          title="移除自定义图片"
          @click="clearCustom"
        >
          移除
        </button>
      </div>
      <!-- 错误提示放在**说明文字之前**，并且加底色。
           原来它排在提示下面、只有 10px、无底色——"上传没反应"时
           用户根本不会看到这一行，于是以为功能坏了。 -->
      <p
        v-if="customError"
        class="tint-error ink-error mt-1.5 rounded px-2 py-1 text-[11px] leading-relaxed"
      >
        {{ customError }}
      </p>
      <p class="ink-subtle mt-1 text-[10px]">
        支持 JPG / PNG / WebP，不超过 {{ UPLOAD_MAX_MB }} MB。图片只存在本机浏览器，不会上传。
      </p>

      <!-- 不透明度 -->
      <div class="mt-3">
        <div class="mb-1 flex items-center justify-between">
          <span class="text-[11px] font-medium">壁纸浓度</span>
          <span class="ink-subtle text-[10px] tabular-nums">{{ Math.round(opacity * 100) }}%</span>
        </div>
        <input
          :value="opacity"
          type="range"
          class="range range-xs"
          :min="OPACITY_MIN"
          :max="OPACITY_MAX"
          step="0.05"
          :disabled="!active"
          @input="setOpacity(Number(($event.target as HTMLInputElement).value))"
        />
        <!-- 必须说明上限的来源：它不是随便定的，
             而是"卡片上的文字仍要 ≥4.5:1"推出来的。 -->
        <p class="ink-subtle mt-1 text-[10px] leading-relaxed">
          上限 {{ Math.round(OPACITY_MAX * 100) }}%：再高会让卡片上的次要文字对比度低于
          4.5:1（无障碍下限）。
        </p>
      </div>

      <button
        v-if="active"
        type="button"
        class="btn btn-ghost btn-xs mt-2 w-full"
        @click="disable"
      >
        关闭壁纸
      </button>
    </div>
  </details>
</template>

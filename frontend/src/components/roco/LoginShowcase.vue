<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import AttributeChip from '@/components/roco/AttributeChip.vue'

/** 登录页右侧的动态展示。
 *
 *  形式：3D 封面流（coverflow）——精灵立绘横排轮转，中间那张正对镜头、
 *  左右两张退到远处并转向，下方配一条五 Agent 研究流水线。
 *  刻意不用满天飞的旋转/漂浮：登录页旁边放一个持续转圈的东西，看久了
 *  既晃眼又抢注意力，而这里的重点是「这个系统能干什么」。
 *
 *  数据来自 ``/api/v1/public/showcase``（无需登录）。取不到时整块退化成
 *  渐变底 —— 登录页绝不能因为展示数据拉不到就打不开，那是本末倒置。
 *
 *  动画只用 CSS 的 transform/opacity/filter 与一个 setInterval 推进索引：
 *  合成器线程跑过渡，主线程只负责每 5 秒换一次内容，输入框打字不掉帧。
 */

interface ShowcasePet {
  name: string
  no: number
  attributes: string[]
  image_url: string
  total: number
}

const CYCLE_MS = 5000

const pets = ref<ShowcasePet[]>([])
const stats = ref({ pets: 0, lineups: 0 })
const index = ref(0)
let timer: ReturnType<typeof setInterval> | undefined

onMounted(async () => {
  try {
    const response = await fetch('/api/v1/public/showcase')
    const payload = await response.json()
    if (payload?.code === 200 && payload.data) {
      pets.value = payload.data.pets || []
      stats.value = payload.data.stats || { pets: 0, lineups: 0 }
    }
  } catch {
    // 展示数据拉不到不影响登录
  }

  if (pets.value.length > 1) {
    timer = setInterval(() => {
      index.value = (index.value + 1) % pets.value.length
    }, CYCLE_MS)
  }
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

/** 舞台是一个 3D 封面流：左边上一只、中间当前、右边下一只。
 *
 *  key 用**精灵在数组里的下标**而不是「位置」：这样 index 前进 1 时，
 *  Vue 会复用原有的 DOM 节点、只改变 CSS 变量，于是卡片是"滑过去"
 *  而不是"销毁重建"——过渡动画才有意义。若按位置做 key，每轮三个
 *  节点全部重建，就只能看到入场动画，没有流动感。
 *
 *  几何量在这里算好再以 CSS 变量下发，避免在样式里用 abs() 之类的
 *  新函数（abs() 是较新的 CSS 特性，旧浏览器直接忽略整条 transform）。
 */
const slots = computed(() => {
  const total = pets.value.length
  if (!total) return []

  // 摆 5 个位置（-2…+2）而不是 3 个：多出来的两个在画面外缘，
  // 正好让新卡"从远处滑进来"、旧卡"滑出去"——只摆 3 个的话，
  // 新节点一创建就已经站在最终位置上，看不到入场过程。
  //
  // 同时位置数不能超过精灵总数：取模会让两个位置落在同一只身上，
  // key 重复，Vue 会警告且过渡错乱。精灵少就少摆。
  const offsets =
    total >= 5
      ? [-2, -1, 0, 1, 2]
      : total === 4
        ? [-2, -1, 0, 1]
        : total === 3
          ? [-1, 0, 1]
          : total === 2
            ? [-1, 0]
            : [0]

  return offsets.map((offset) => {
    const i = (index.value + offset + total) % total
    const distance = Math.abs(offset)
    return {
      key: i,
      pet: pets.value[i],
      isCenter: distance === 0,
      style: {
        // 偏移量是**卡片自身宽度的百分比**——translateX 的百分比基准是元素
        // 自身宽度，不是容器宽度。所以必须 >100% 才能把侧边的卡推到中间那张
        // 之外；用 52% 的话它们几乎整个躲在中间那张背后，看起来像"只有一张图"。
        '--slot-x': `${offset * 104}%`,
        '--slot-scale': String(1 - distance * 0.2),
        '--slot-rot': `${offset * -34}deg`,
        // 负值 = 往屏幕里退。配合 .coverflow 的 perspective，
        // 侧边的卡自然变小、变虚，形成景深。
        '--slot-depth': `${-distance * 90}px`,
        '--slot-blur': `${distance * 1.2}px`,
        // 距离 2 的那张接近全透明：新卡刚创建时就在这个位置，
        // 若还有可见度就会"凭空冒出来"；从 0 开始淡入才自然。
        '--slot-opacity': String(Math.max(0, 1 - distance * 0.45)),
        '--slot-z': String(10 - distance),
        '--slot-info': distance === 0 ? '1' : '0',
      } as Record<string, string>,
    }
  })
})

/** 五个 Agent 的流水线，顺序与后端 LangGraph 拓扑一致。 */
const pipeline = [
  { key: 'planner', label: 'Planner', note: '拆解问题' },
  { key: 'researcher', label: 'Researcher', note: '并行取证' },
  { key: 'analyst', label: 'Analyst', note: '归纳克制' },
  { key: 'writer', label: 'Writer', note: '撰写攻略' },
  { key: 'reviewer', label: 'Reviewer', note: '审校修订' },
]
</script>

<template>
  <div class="stage relative h-full w-full overflow-hidden">
    <!-- 背景：顶部聚光 + 底部暖光，纯 CSS 渐变，无额外图层 -->
    <div class="stage-glow pointer-events-none absolute inset-0" />

    <div class="relative z-10 flex h-full flex-col px-8 py-10 xl:px-12">
      <!-- ============ 标题 ============ -->
      <div class="shrink-0">
        <p class="text-[11px] font-medium tracking-[0.3em] text-white/45 uppercase">
          Roco Kingdom · World
        </p>
        <h2 class="mt-2 text-2xl font-bold tracking-wide text-white xl:text-3xl">
          洛克王国：世界
        </h2>
        <p class="mt-1 text-sm text-white/60">PvP 阵容攻略 Agent</p>
      </div>

      <!-- ============ 舞台：精灵封面流 ============ -->
      <div class="relative flex min-h-0 flex-1 items-center justify-center">
        <div v-if="slots.length" class="coverflow">
          <div
            v-for="slot in slots"
            :key="slot.key"
            class="slot"
            :class="{ 'slot-center': slot.isCenter }"
            :style="slot.style"
          >
            <img
              class="slot-art"
              :src="slot.pet.image_url"
              :alt="slot.pet.name"
              loading="eager"
              decoding="async"
            />

            <!-- 名字/属性只跟着中间那张走：侧边卡缩得很小，写上去只是噪点 -->
            <div class="slot-info" aria-hidden="false">
              <div class="flex items-baseline justify-center gap-2">
                <span class="text-base font-bold text-white">{{ slot.pet.name }}</span>
                <span class="font-mono text-[10px] text-white/40">
                  No.{{ String(slot.pet.no).padStart(3, '0') }}
                </span>
              </div>
              <div class="mt-2 flex flex-wrap items-center justify-center gap-1">
                <AttributeChip
                  v-for="attr in slot.pet.attributes"
                  :key="attr"
                  :type="attr"
                  size="xs"
                />
              </div>
              <div v-if="slot.pet.total" class="mt-2 text-[11px] text-white/45">
                种族值合计
                <span class="ml-1 font-mono text-sm font-bold text-white/85 tabular-nums">
                  {{ slot.pet.total }}
                </span>
              </div>
            </div>
          </div>
        </div>

        <!-- 数据没到：留一块同尺寸的空舞台，避免布局跳动 -->
        <div v-else class="h-56 w-40 rounded-3xl border border-white/5 bg-white/[0.02]" />
      </div>

      <!-- ============ 五 Agent 流水线 ============ -->
      <div class="shrink-0">
        <div class="mb-4 flex items-center gap-3">
          <span class="text-[11px] font-medium tracking-wider text-white/50">五 Agent 深度研究</span>
          <span class="h-px flex-1 bg-white/10" />
        </div>

        <ol class="relative flex items-start justify-between">
          <!-- 贯穿的细线，藏在圆点后面 -->
          <span class="absolute top-[5px] left-2 right-2 h-px bg-white/12" aria-hidden="true" />

          <li
            v-for="(node, i) in pipeline"
            :key="node.key"
            class="relative flex flex-1 flex-col items-center text-center"
          >
            <span
              class="dot relative z-10 mb-2 block h-[11px] w-[11px] rounded-full border"
              :style="{ animationDelay: `${i * 0.55}s` }"
            />
            <span class="text-[10px] font-semibold text-white/70">{{ node.label }}</span>
            <span class="mt-0.5 text-[9px] text-white/35">{{ node.note }}</span>
          </li>
        </ol>

        <!-- 真实数据规模：比任何形容词都有说服力 -->
        <div v-if="stats.pets" class="mt-6 flex items-center gap-5 text-[11px] text-white/45">
          <span>
            <span class="font-mono text-base font-bold text-white/90 tabular-nums">
              {{ stats.pets }}
            </span>
            只精灵
          </span>
          <span>
            <span class="font-mono text-base font-bold text-white/90 tabular-nums">
              {{ stats.lineups }}
            </span>
            套投稿阵容
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* ------------------------------------------------------------------ 背景 */

.stage {
  background: linear-gradient(165deg, oklch(20% 0.05 265), oklch(13% 0.04 250));
}

.stage-glow {
  background:
    radial-gradient(ellipse 70% 45% at 50% 22%, oklch(62% 0.17 265 / 0.28), transparent 70%),
    radial-gradient(ellipse 60% 40% at 50% 100%, oklch(58% 0.14 200 / 0.22), transparent 70%);
}

/* ------------------------------------------------------------------ 封面流 */

/* 透视放在容器上，子元素的 translateZ/rotateY 才有景深；
   不给 perspective-origin 的话默认在中心，正好是我们要的视点。 */
.coverflow {
  position: relative;
  width: 100%;
  height: 14rem;
  perspective: 1100px;
  transform-style: preserve-3d;
}

/* 各张卡共用同一套定位：先居中，再按 --slot-x 左右推开、按 --slot-depth 后退、
   按 --slot-rot 转向。transition 只写在 transform/opacity 上——这两个属性能
   交给合成器，每 5 秒换一次索引时主线程不需要重排整棵树。

   blur 挂在里面的 img 上而不是这里：元素一旦有 filter（或 opacity < 1），
   它就会被当成一个"分组"渲染。.slot 是 3D 场景里的成员，别让它整体变糊。 */
.slot {
  position: absolute;
  top: 50%;
  left: 50%;
  width: 9rem;
  transform: translate(-50%, -50%) translateX(var(--slot-x))
    translateZ(var(--slot-depth)) rotateY(var(--slot-rot)) scale(var(--slot-scale));
  opacity: var(--slot-opacity);
  /* 父容器没开 preserve-3d，子元素的 3D 变换会被各自压平，
     此时绘制顺序由 z-index 决定而不是 z 坐标——不显式指定的话，
     中间那张可能被侧边的卡盖住。 */
  z-index: var(--slot-z);
  transition:
    transform 750ms cubic-bezier(0.22, 1, 0.36, 1),
    opacity 750ms ease;
  will-change: transform, opacity;
}

.slot-art {
  display: block;
  width: 100%;
  height: auto;
  /* 立绘边缘是透明像素，投影比边框更贴合轮廓。
     越靠边的卡越虚，形成景深；模糊量由脚本按距离下发。 */
  filter: blur(var(--slot-blur)) drop-shadow(0 14px 24px rgb(0 0 0 / 0.45));
  transition: filter 750ms ease;
}

/* 名字/属性只对中间那张可见：--slot-info 由脚本按距离下发 0/1，
   用 opacity 过渡而不是 v-if，切换时是淡入淡出而不是突然出现。
   position:absolute 让它脱离文档流、不参与 .slot 的高度计算，
   于是精灵再高也不会把卡片撑出舞台。 */
.slot-info {
  position: absolute;
  top: 100%;
  left: 50%;
  width: 14rem;
  transform: translateX(-50%);
  margin-top: 0.25rem;
  text-align: center;
  opacity: var(--slot-info);
  transition: opacity 500ms ease 120ms;
}

/* 中间那张轻轻呼吸。注意用 translate 属性而不是 transform：
   .slot 的 transform 已经被封面流占用，写 transform 会把它覆盖掉。 */
.slot-center {
  animation: breathe 5s ease-in-out infinite;
}

@keyframes breathe {
  0%, 100% { translate: 0 0; }
  50% { translate: 0 -8px; }
}

/* ------------------------------------------------------------------ 流水线 */

/* 圆点像信号一样依次点亮，暗示"流水线在跑"而不是五个静态标签 */
.dot {
  border-color: oklch(100% 0 0 / 0.25);
  background: oklch(100% 0 0 / 0.08);
  animation: dot-pulse 2.75s ease-in-out infinite;
}

@keyframes dot-pulse {
  0%, 100% {
    background: oklch(100% 0 0 / 0.08);
    border-color: oklch(100% 0 0 / 0.25);
    box-shadow: none;
  }
  20% {
    background: oklch(72% 0.16 265);
    border-color: oklch(82% 0.12 265);
    box-shadow: 0 0 0 4px oklch(72% 0.16 265 / 0.18);
  }
  45% {
    background: oklch(100% 0 0 / 0.08);
    border-color: oklch(100% 0 0 / 0.25);
    box-shadow: none;
  }
}

/* ------------------------------------------------------------------ 无障碍 */

/* 尊重系统的"减少动态效果"设置：前庭功能障碍的用户会因为持续运动而头晕。
   关掉动画后，左右两张直接隐藏、中间一张静止，信息一点没少。 */
@media (prefers-reduced-motion: reduce) {
  .slot-center,
  .dot {
    animation: none !important;
  }

  .slot {
    transition: none;
  }

  /* 只留中间那张：:not(.slot-center) 精确命中左右两张 */
  .slot:not(.slot-center) {
    display: none;
  }
}
</style>

<script setup lang="ts">
/** 壁纸层。
 *
 *  ★ 这里**只有壁纸**，没有全屏磨砂层。
 *  一开始的写法是一张 `.glass-canvas` 铺满屏、对整个壁纸做 blur，
 *  结果是"壁纸功能形同虚设"——不管选哪张壁纸，看起来都是同一片灰雾，
 *  而页面元素反倒没有玻璃质感。
 *
 *  现在的分工：
 *    - 壁纸：清晰可见（这一层）
 *    - 毛玻璃：做在**页面结构元素**上（.glass-region / .glass-chrome /
 *      .glass-panel / .surface），由各元素对自己的背景做模糊
 *
 *  固定定位 + pointer-events:none：不参与滚动、不挡交互。
 *  使用方必须把内容容器设为 relative + z-10，否则会被这一层盖住。
 */
</script>

<template>
  <div class="wallpaper" aria-hidden="true" />
</template>

import { computed, ref, watch } from 'vue'
import { useTheme } from './useTheme'
import { CUSTOM_ID, NONE_ID, findPreset, presetCss } from './wallpapers'

/** 壁纸状态。
 *
 * 为什么自定义图片放 IndexedDB 而不是 localStorage
 * ------------------------------------------------
 * localStorage 只有约 5MB，且**同一 origin 的所有 key 共享配额**。
 * 一张 3MB 的照片转成 base64 约 4MB，写进去会直接抛 QuotaExceededError；
 * 更糟的是它可能把同 origin 的 `rag_token`（登录态）一起挤掉——
 * 用户只是换了个壁纸，结果被登出，这种故障极难联想到原因。
 * IndexedDB 存二进制 Blob 没有这个问题，也不做 base64 膨胀（省 33%）。
 */

const STORAGE_KEY = 'rag_wallpaper'
const DB_NAME = 'rag-wallpaper'
const STORE = 'images'
const CUSTOM_KEY = 'custom'

/** 与 scripts/glass_budget.py 的 DESIGN 保持一致。
 *
 *  上限 0.85 由 **chrome（顶栏/侧栏）** 决定，不是内容区：
 *  内容区现在**没有玻璃层**（壁纸 1:1 原样显示），所以它不构成限制；
 *  而顶栏的玻璃是 82%（business），实测最多支撑到 87% 浓度
 *  （再高时顶栏上的最弱文字只有 4.20:1）。取 85% 留一点余量。 */
export const OPACITY_MIN = 0.15
export const OPACITY_MAX = 0.85
export const OPACITY_DEFAULT = 0.85

/** 上传图片的大小上限。超过这个值就不必存了——
 *  壁纸是被模糊+半透明盖住的背景，4K 原图没有任何可见收益。 */
export const UPLOAD_MAX_MB = 8

type Source = 'none' | 'preset' | 'custom'

interface Stored {
  source: Source
  presetId: string
  opacity: number
}

/** 允许的图片扩展名。
 *
 *  为什么要靠扩展名兜底：`File.type` 由浏览器推断，**可能是空串**——
 *  拖拽、U 盘、某些系统上都是空的。只按 `type.startsWith('image/')` 判定
 *  会把合法图片拒掉，而错误提示只有 10px，用户很容易没看见，
 *  只觉得"上传没反应"。（实测确认：`new File([png], 'x.png', {type:''})`
 *  的 type 就是空串，但它是完全合法的 PNG。）
 *
 *  注意扩展名只能作为 **MIME 为空时的兜底**，不能作为唯一依据：
 *  真正决定"能不能用"的是下面 decodeImage() 的解码结果。 */
const IMAGE_EXTENSIONS = ['png', 'jpg', 'jpeg', 'webp', 'gif', 'bmp', 'avif', 'svg']

function hasImageExtension(name: string): boolean {
  const dot = name.lastIndexOf('.')
  if (dot < 0) return false
  return IMAGE_EXTENSIONS.includes(name.slice(dot + 1).toLowerCase())
}

/** 真正解码一次图片，确认浏览器能渲染它。
 *
 *  ★ 这一步不能省。`URL.createObjectURL()` 对**任何** File 都成功，
 *  所以"不报错"不等于"能用"：一个伪装成 .jpg 的损坏文件会让
 *  source 变成 custom、面板显示文件名、localStorage 也记下 custom，
 *  但图片永远不渲染 —— 背景什么都没有，且**没有任何报错**。
 *  这正是用户反馈的"上传了但没替换成功"。
 *
 *  用 createImageBitmap 而不是 `new Image()`：它不依赖 DOM，
 *  且失败时会 reject，错误更容易捕获。老浏览器没有这个 API 时
 *  退回 `new Image()`。 */
async function decodeImage(file: File): Promise<boolean> {
  if (typeof createImageBitmap === 'function') {
    try {
      const bitmap = await createImageBitmap(file)
      // 必须显式释放：bitmap 持有解码后的像素内存，
      // 一张 4K 图约 33MB，不释放会在连续上传时累积。
      bitmap.close()
      return true
    } catch {
      return false
    }
  }

  return new Promise<boolean>((resolve) => {
    const url = URL.createObjectURL(file)
    const img = new Image()
    const done = (ok: boolean): void => {
      URL.revokeObjectURL(url)
      resolve(ok)
    }
    img.onload = () => done(true)
    img.onerror = () => done(false)
    // 解码卡住时不能无限等（损坏文件有时既不 load 也不 error）
    window.setTimeout(() => done(false), 5000)
    img.src = url
  })
}

const themeCtx = useTheme()

const source = ref<Source>('none')
const presetId = ref<string>('indigo')
const opacity = ref(OPACITY_DEFAULT)
const customUrl = ref<string>('')
const customName = ref('')
const customError = ref('')
const loading = ref(true)

/** 当前实际要绘制的 CSS background-image 值。 */
const currentImage = computed(() => {
  if (source.value === 'none') return 'none'
  if (source.value === 'custom') return customUrl.value ? `url("${customUrl.value}")` : 'none'
  const preset = findPreset(presetId.value)
  if (!preset) return 'none'
  return presetCss(preset, themeCtx.theme.value)
})

/** 是否处于"有壁纸"状态（用于 UI 与探针）。 */
const active = computed(() => currentImage.value !== 'none')

// ---------------------------------------------------------------- IndexedDB

function openDb(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, 1)
    request.onupgradeneeded = () => {
      const db = request.result
      if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE)
    }
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => reject(request.error)
  })
}

async function idbPut(blob: Blob): Promise<void> {
  const db = await openDb()
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(STORE, 'readwrite')
    tx.objectStore(STORE).put(blob, CUSTOM_KEY)
    tx.oncomplete = () => resolve()
    tx.onerror = () => reject(tx.error)
  })
  db.close()
}

async function idbGet(): Promise<Blob | undefined> {
  const db = await openDb()
  const blob = await new Promise<Blob | undefined>((resolve, reject) => {
    const tx = db.transaction(STORE, 'readonly')
    const request = tx.objectStore(STORE).get(CUSTOM_KEY)
    request.onsuccess = () => resolve(request.result as Blob | undefined)
    request.onerror = () => reject(request.error)
  })
  db.close()
  return blob
}

async function idbDelete(): Promise<void> {
  const db = await openDb()
  await new Promise<void>((resolve, reject) => {
    const tx = db.transaction(STORE, 'readwrite')
    tx.objectStore(STORE).delete(CUSTOM_KEY)
    tx.oncomplete = () => resolve()
    tx.onerror = () => reject(tx.error)
  })
  db.close()
}

// ---------------------------------------------------------------- 应用与持久化

function revokeCustom(): void {
  if (customUrl.value.startsWith('blob:')) URL.revokeObjectURL(customUrl.value)
}

/** 把状态写进 CSS 变量。
 *
 *  只写两个变量，而不是给每个用到壁纸的地方加样式：
 *  .wallpaper 这一个 @utility 负责绘制，改壁纸不需要碰任何组件。 */
function apply(): void {
  const root = document.documentElement
  root.style.setProperty('--wallpaper-image', currentImage.value)
  root.style.setProperty('--wallpaper-opacity', String(opacity.value))
}

function persist(): void {
  try {
    const payload: Stored = {
      source: source.value,
      presetId: presetId.value,
      opacity: opacity.value,
    }
    localStorage.setItem(STORAGE_KEY, JSON.stringify(payload))
  } catch {
    // 隐私模式下 localStorage 可能不可写。壁纸只是外观，不该因此报错。
  }
}

watch([currentImage, opacity], apply, { immediate: true })
watch([source, presetId, opacity], persist)

/** 主题切换时预设要换一套（浅色/深色渐变不同）。
 *  currentImage 是 computed，主题变化会带动它，上面的 watch 自动重绘。 */

async function restore(): Promise<void> {
  let stored: Stored | null = null
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) stored = JSON.parse(raw) as Stored
  } catch {
    stored = null
  }

  if (stored) {
    if (stored.source === 'none' || stored.source === 'preset' || stored.source === 'custom') {
      source.value = stored.source
    }
    if (typeof stored.presetId === 'string') presetId.value = stored.presetId
    if (typeof stored.opacity === 'number' && Number.isFinite(stored.opacity)) {
      // 夹紧：旧版本可能存了超出当前安全范围的值
      opacity.value = Math.min(OPACITY_MAX, Math.max(OPACITY_MIN, stored.opacity))
    }
  }

  if (source.value === 'custom') {
    try {
      const blob = await idbGet()
      if (blob) {
        customUrl.value = URL.createObjectURL(blob)
      } else {
        // 标记为自定义但图片没了（用户清了站点数据）：退回无壁纸，
        // 而不是留一个坏引用让整页变成空白背景。
        source.value = 'none'
      }
    } catch {
      source.value = 'none'
    }
  }

  loading.value = false
}

void restore()

// ---------------------------------------------------------------- 对外接口

export function useWallpaper() {
  function choosePreset(id: string): void {
    customError.value = ''
    if (id === NONE_ID) {
      source.value = 'none'
      return
    }
    if (id === CUSTOM_ID) {
      // 选择"自定义"只是切到该来源；没有图片时保持无壁纸，
      // 由 UI 引导用户去上传。
      source.value = customUrl.value ? 'custom' : 'none'
      return
    }
    if (findPreset(id)) {
      presetId.value = id
      source.value = 'preset'
    }
  }

  async function uploadCustom(file: File): Promise<boolean> {
    customError.value = ''

    // MIME 可能为空串（拖拽 / U 盘 / 某些系统），此时靠扩展名兜底。
    // 两道都不过才判为非图片。
    const mimeOk = file.type.startsWith('image/')
    const extOk = hasImageExtension(file.name)
    if (!mimeOk && !extOk) {
      customError.value = `请选择图片文件（支持 ${IMAGE_EXTENSIONS.slice(0, 5).join(' / ')}）。`
      return false
    }

    if (file.size > UPLOAD_MAX_MB * 1024 * 1024) {
      customError.value = `图片不能超过 ${UPLOAD_MAX_MB} MB（当前 ${(file.size / 1024 / 1024).toFixed(1)} MB）。`
      return false
    }

    // ★ 先确认浏览器真的能解码，再写库、再切状态。
    // 顺序很重要：反过来做的话，一个坏文件会先把 source 改成 custom、
    // 界面显示"已选择"，然后背景空白——用户完全不知道发生了什么。
    if (!(await decodeImage(file))) {
      customError.value = '这个文件无法作为图片打开（可能已损坏或格式不受支持）。'
      return false
    }

    try {
      await idbPut(file)
    } catch {
      customError.value = '图片保存失败（浏览器可能禁用了本地存储）。'
      return false
    }
    revokeCustom()
    customUrl.value = URL.createObjectURL(file)
    customName.value = file.name
    source.value = 'custom'
    return true
  }

  async function clearCustom(): Promise<void> {
    revokeCustom()
    customUrl.value = ''
    customName.value = ''
    try {
      await idbDelete()
    } catch {
      // 删不掉也不影响使用：下次上传会覆盖同一个 key
    }
    if (source.value === 'custom') source.value = 'none'
  }

  function setOpacity(value: number): void {
    opacity.value = Math.min(OPACITY_MAX, Math.max(OPACITY_MIN, value))
  }

  /** 关闭壁纸（回到纯主题底色）。 */
  function disable(): void {
    source.value = 'none'
  }

  return {
    source,
    presetId,
    opacity,
    customUrl,
    customName,
    customError,
    loading,
    active,
    choosePreset,
    uploadCustom,
    clearCustom,
    setOpacity,
    disable,
  }
}

export async function api<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch('/api' + path, {
    credentials: 'include', ...options,
    headers: options.body instanceof FormData ? options.headers : { 'Content-Type': 'application/json', ...options.headers },
  })
  const data = await response.json().catch(() => ({}))
  if (!response.ok) {
    const detail = data.detail
    throw new Error(typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((d: any) => d.msg).join('；') : detail?.message || `请求失败 (${response.status})`)
  }
  return data
}

export const json = (method: string, value?: unknown): RequestInit => ({ method, body: value === undefined ? undefined : JSON.stringify(value) })
export const bytes = (value: number | string = 0) => {
  const n = Number(value)
  return n >= 1024 ** 3 ? (n / 1024 ** 3).toFixed(2) + ' GB' : n >= 1024 ** 2 ? (n / 1024 ** 2).toFixed(1) + ' MB' : n >= 1024 ? (n / 1024).toFixed(1) + ' KB' : n + ' B'
}
export const date = (value: string) => value ? new Date(value).toLocaleString('zh-CN') : '—'

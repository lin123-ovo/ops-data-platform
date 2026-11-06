import axios from 'axios'
import { ElMessage } from 'element-plus'

// 开发环境的 /api/v1 请求由 vite.config.ts 代理到 127.0.0.1:8000。
// 页面和后端的桥梁在这里，后续 api/modules 文件只补充具体业务路径。
const DEFAULT_API_BASE_URL = '/api/v1'
export const VECTOR_WRITE_TIMEOUT_MS = 180000

export const apiBaseUrl = import.meta.env.VITE_API_BASE_URL || DEFAULT_API_BASE_URL
export const backendOrigin = apiBaseUrl.endsWith('/api/v1')
  ? apiBaseUrl.slice(0, -'/api/v1'.length) || ''
  : ''

function extractErrorMessage(error: unknown): string {
  if (axios.isAxiosError(error)) {
    if (error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT') {
      return error.config?.timeout === VECTOR_WRITE_TIMEOUT_MS
        ? '请求等待超时，后端可能仍在处理。请刷新确认文档或片段状态，避免重复提交。'
        : '请求超时，请稍后重试。'
    }
    const detail = error.response?.data?.detail
    if (typeof detail === 'string' && detail.trim()) {
      return detail
    }

    if (Array.isArray(detail)) {
      return detail.map((item) => item?.msg || JSON.stringify(item)).join('；')
    }

    return error.message || '请求失败'
  }

  if (error instanceof Error) {
    return error.message
  }

  return '请求失败'
}

export function notifyError(error: unknown, fallback = '操作失败'): string {
  const detail = extractErrorMessage(error) || fallback
  const english = typeof document !== 'undefined' && document.documentElement.lang === 'en'
  const hasChinese = /[\u3400-\u9fff]/
  const message = english && hasChinese.test(detail)
    ? (hasChinese.test(fallback) ? 'Request failed. Try again.' : fallback)
    : detail
  ElMessage.error(message)
  return message
}

export const http = axios.create({
  baseURL: apiBaseUrl,
  timeout: 30000,
})

http.interceptors.response.use(
  (response) => response,
  (error) => Promise.reject(error),
)

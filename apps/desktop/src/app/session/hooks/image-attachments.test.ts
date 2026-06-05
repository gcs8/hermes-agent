import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ComposerAttachment } from '@/store/composer'

import { attachImageForSubmit } from './use-prompt-actions'

type RequestGateway = <T>(method: string, params?: Record<string, unknown>) => Promise<T>

function imageAttachment(path: string, extra: Partial<ComposerAttachment> = {}): ComposerAttachment {
  return {
    id: `image:${path}`,
    kind: 'image',
    label: 'cat.png',
    path,
    ...extra
  }
}

describe('attachImageForSubmit', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: undefined
    })
  })

  it('keeps using image.attach when the gateway can resolve the path', async () => {
    const requestGateway = vi.fn(async method => {
      if (method === 'image.attach') {
        return { attached: true, path: '/remote/shared/cat.png' }
      }

      throw new Error(`unexpected method: ${method}`)
    }) as unknown as RequestGateway

    const result = await attachImageForSubmit('sid', imageAttachment('/remote/shared/cat.png'), requestGateway)

    expect(result.path).toBe('/remote/shared/cat.png')
    expect(requestGateway).toHaveBeenCalledTimes(1)
    expect(requestGateway).toHaveBeenCalledWith('image.attach', {
      path: '/remote/shared/cat.png',
      session_id: 'sid'
    })
  })

  it('uploads directly when remote mode prefers byte upload over local paths', async () => {
    const localPath = 'C:\\Users\\gcs8\\AppData\\Roaming\\Hermes\\composer-images\\cat.png'
    const dataUrl = 'data:image/png;base64,iVBORw0KGgo='

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'image.upload') {
        return { attached: true, path: '/home/gcs8/.hermes/cache/images/img_remote_first.png' }
      }

      throw new Error(`unexpected method: ${method}`)
    }) as unknown as RequestGateway

    const result = await attachImageForSubmit(
      'sid',
      imageAttachment(localPath, { previewUrl: dataUrl }),
      requestGateway,
      { preferUpload: true }
    )

    expect(result.path).toBe('/home/gcs8/.hermes/cache/images/img_remote_first.png')
    expect(requestGateway).toHaveBeenCalledTimes(1)
    expect(requestGateway).toHaveBeenCalledWith('image.upload', {
      data_url: dataUrl,
      filename: 'cat.png',
      session_id: 'sid'
    })
  })

  it('falls back to image.attach_bytes when image.upload is unavailable on the gateway', async () => {
    const localPath = 'C:\\Users\\gcs8\\AppData\\Roaming\\Hermes\\composer-images\\cat.png'
    const dataUrl = 'data:image/png;base64,iVBORw0KGgo='

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'image.upload') {
        throw new Error('unknown method: image.upload')
      }

      if (method === 'image.attach_bytes') {
        return { attached: true, path: '/home/gcs8/.hermes/cache/images/img_bytes.png' }
      }

      throw new Error(`unexpected method: ${method}`)
    }) as unknown as RequestGateway

    const result = await attachImageForSubmit(
      'sid',
      imageAttachment(localPath, { previewUrl: dataUrl }),
      requestGateway,
      { preferUpload: true }
    )

    expect(result.path).toBe('/home/gcs8/.hermes/cache/images/img_bytes.png')
    expect(requestGateway).toHaveBeenNthCalledWith(1, 'image.upload', {
      data_url: dataUrl,
      filename: 'cat.png',
      session_id: 'sid'
    })
    expect(requestGateway).toHaveBeenNthCalledWith(2, 'image.attach_bytes', {
      content_base64: 'iVBORw0KGgo=',
      filename: 'cat.png',
      session_id: 'sid'
    })
  })

  it('uploads local bytes when a gateway cannot resolve the desktop path fallback', async () => {
    const localPath = 'C:\\Users\\gcs8\\AppData\\Roaming\\Hermes\\composer-images\\cat.png'
    const dataUrl = 'data:image/png;base64,iVBORw0KGgo='
    const readFileDataUrl = vi.fn(async () => dataUrl)

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'image.attach') {
        throw new Error(`image not found: ${localPath}`)
      }

      if (method === 'image.upload') {
        return { attached: true, path: '/home/gcs8/.hermes/cache/images/img_abc123.png' }
      }

      throw new Error(`unexpected method: ${method}`)
    }) as unknown as RequestGateway

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { readFileDataUrl }
    })

    const result = await attachImageForSubmit('sid', imageAttachment(localPath), requestGateway)

    expect(result.path).toBe('/home/gcs8/.hermes/cache/images/img_abc123.png')
    expect(readFileDataUrl).toHaveBeenCalledWith(localPath)
    expect(requestGateway).toHaveBeenNthCalledWith(1, 'image.attach', {
      path: localPath,
      session_id: 'sid'
    })
    expect(requestGateway).toHaveBeenNthCalledWith(2, 'image.upload', {
      data_url: dataUrl,
      filename: 'cat.png',
      session_id: 'sid'
    })
  })

  it('does not upload for non-missing-path attachment errors', async () => {
    const readFileDataUrl = vi.fn(async () => 'data:image/png;base64,iVBORw0KGgo=')

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'image.attach') {
        throw new Error('unsupported image: cat.txt')
      }

      throw new Error(`unexpected method: ${method}`)
    }) as unknown as RequestGateway

    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { readFileDataUrl }
    })

    await expect(attachImageForSubmit('sid', imageAttachment('/tmp/cat.txt'), requestGateway)).rejects.toThrow(
      'unsupported image: cat.txt'
    )

    expect(readFileDataUrl).not.toHaveBeenCalled()
    expect(requestGateway).toHaveBeenCalledTimes(1)
  })
})

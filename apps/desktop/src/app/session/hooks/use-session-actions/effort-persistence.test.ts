import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { $newChatProfile, $newChatRoute } from '@/store/profile'
import {
  $currentReasoningEffort,
  $currentReasoningEffortWire,
  setCurrentModelSource,
  setCurrentReasoningEffort,
  setCurrentReasoningEffortWire
} from '@/store/session'

import { createGatewaySession, desktopSessionCreateParams } from './session-create-request'
import { applyRuntimeInfo, applyStoredSessionPreviewRuntimeInfo } from './utils'

import { useSessionActions } from './index'

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ensureGatewayProfile: vi.fn(async () => undefined)
}))

const EFFORT_KEY = 'hermes.desktop.composer.reasoning-effort'

function freshDraftActions() {
  return renderHook(() =>
    useSessionActions({
      activeSessionId: null,
      activeSessionIdRef: { current: null },
      busyRef: { current: false },
      creatingSessionRef: { current: false },
      ensureSessionState: () => createClientSessionState(null),
      getRouteToken: () => 'fresh',
      getRoutedStoredSessionId: () => null,
      navigate: vi.fn(),
      requestGateway: vi.fn(async () => ({}) as never),
      resetViewSync: vi.fn(),
      routedSessionId: null,
      runtimeIdByStoredSessionIdRef: { current: new Map() },
      selectedStoredSessionId: null,
      selectedStoredSessionIdRef: { current: null },
      sessionStateByRuntimeIdRef: { current: new Map() },
      syncSessionStateToView: vi.fn(),
      updateSessionState: () => createClientSessionState(null)
    })
  )
}

describe('viewed effort does not become a new-chat preference, regression for #145299', () => {
  beforeEach(() => {
    $newChatProfile.set(null)
    $newChatRoute.set(null)
    setCurrentModelSource('manual')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
    setCurrentReasoningEffort('')
    setCurrentReasoningEffortWire('')
    setCurrentModelSource('')
  })

  it.each(['high', ''])('cold preview and resume preserve the saved effort choice %s', picked => {
    setCurrentReasoningEffort(picked)
    const saved = localStorage.getItem(EFFORT_KEY)

    applyStoredSessionPreviewRuntimeInfo(undefined, 'stored-viewed')
    expect($currentReasoningEffort.get()).toBe('')
    expect(localStorage.getItem(EFFORT_KEY)).toBe(saved)

    applyRuntimeInfo({ reasoning_effort: 'low' })
    expect($currentReasoningEffort.get()).toBe('low')
    expect(localStorage.getItem(EFFORT_KEY)).toBe(saved)
  })

  it.each(['high', ''])('a fresh draft sends the saved effort choice %s, not the viewed level', async picked => {
    const { result } = freshDraftActions()
    act(() => {
      setCurrentReasoningEffort(picked)
      applyRuntimeInfo({ reasoning_effort: 'low', reasoning_effort_wire: 'low' })
      result.current.startFreshSessionDraft()
    })

    expect($currentReasoningEffort.get()).toBe(picked)
    expect($currentReasoningEffortWire.get()).toBe('')
    const params = await desktopSessionCreateParams('', null)
    const request = vi.fn(async () => ({ session_id: 'new-runtime' }) as never)
    await createGatewaySession(null, params, request)
    expect(request).toHaveBeenCalledWith('session.create', params)

    if (picked) {
      expect(params).toMatchObject({ reasoning_effort: picked })
    } else {
      expect(params).not.toHaveProperty('reasoning_effort')
    }
  })

  it('a deliberate pick after viewing becomes the next new-chat choice', () => {
    const { result } = freshDraftActions()
    act(() => {
      setCurrentReasoningEffort('high')
      applyRuntimeInfo({ reasoning_effort: 'low' })
      setCurrentReasoningEffort('xhigh')
      result.current.startFreshSessionDraft()
    })

    expect($currentReasoningEffort.get()).toBe('xhigh')
    expect(localStorage.getItem(EFFORT_KEY)).toBe('xhigh')
  })

  it.each(['high', ''])(
    'a split new chat uses the saved effort choice %s without changing the viewed chat',
    async picked => {
      setCurrentReasoningEffort(picked)
      applyRuntimeInfo({ reasoning_effort: 'low' })

      const params = await desktopSessionCreateParams('', null)

      expect($currentReasoningEffort.get()).toBe('low')

      if (picked) {
        expect(params).toMatchObject({ reasoning_effort: picked })
      } else {
        expect(params).not.toHaveProperty('reasoning_effort')
      }
    }
  )

  it('keeps an explicit pick in memory when browser storage is unavailable', () => {
    const { result } = freshDraftActions()
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('storage unavailable')
    })
    act(() => {
      setCurrentReasoningEffort('high')
      applyRuntimeInfo({ reasoning_effort: 'low' })
      result.current.startFreshSessionDraft()
    })

    expect($currentReasoningEffort.get()).toBe('high')
  })
})

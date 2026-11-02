import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { en } from '@/i18n/en'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $activeGatewayProfile, $newChatProfile } from '@/store/profile'
import {
  $activeSessionId,
  $connection,
  $currentModel,
  $currentReasoningEffort,
  $selectedStoredSessionId,
  getComposerReasoningEffort,
  markComposerSelectionManual,
  restoreComposerReasoningEffort,
  setCurrentModel,
  setCurrentReasoningEffort,
  setCurrentReasoningEffortTransient
} from '@/store/session'
import { deferred } from '@/test/deferred'

import { desktopSessionCreateParams } from '../use-session-actions/session-create-request'
import { applyRuntimeInfo } from '../use-session-actions/utils'

import { useSlashCommand } from './slash'
import type { GatewayRequest } from './utils'

vi.mock('@/store/profile', async importOriginal => ({
  ...(await importOriginal()),
  ensureGatewayProfile: vi.fn(async () => undefined)
}))
afterEach(cleanup)
beforeEach(() => {
  $activeSessionId.set('runtime-a')
  $selectedStoredSessionId.set('stored-a')
  $connection.set(null)
  $activeGatewayProfile.set('default')
  $newChatProfile.set('default')
  setCurrentReasoningEffort('high')
  setCurrentReasoningEffortTransient('low')
})

function setup(
  requestGateway: GatewayRequest,
  fresh?: { beforePublish?: () => void; afterPublish?: () => void; aborted?: boolean }
) {
  const activeSessionIdRef = { current: fresh ? null : ('runtime-a' as string | null) }
  const selectedStoredSessionIdRef = { current: fresh ? null : ('stored-a' as string | null) }

  $activeSessionId.set(activeSessionIdRef.current)
  $selectedStoredSessionId.set(selectedStoredSessionIdRef.current)

  const createSession = vi.fn(async () => {
    fresh?.beforePublish?.()

    if (!fresh || fresh.aborted) {
      return null
    }

    // Same foreground publication seam as createBackendSessionForSend.
    activeSessionIdRef.current = 'created-runtime'
    selectedStoredSessionIdRef.current = 'created-stored'
    $activeSessionId.set('created-runtime')
    $selectedStoredSessionId.set('created-stored')
    fresh.afterPublish?.()

    return 'created-runtime'
  })

  const appendSessionTextMessage = vi.fn()

  const routedRequest = ((method: string, params?: Record<string, unknown>, timeoutMs?: number) =>
    method === 'shared_metrics.slash_command'
      ? Promise.resolve({})
      : requestGateway(method, params, timeoutMs)) as GatewayRequest

  const hook = renderHook(() =>
    useSlashCommand({
      activeSessionIdRef,
      selectedStoredSessionIdRef,
      busyRef: { current: true },
      copy: en.desktop,
      createBackendSessionForSend: createSession,
      getRoutedStoredSessionId: () => selectedStoredSessionIdRef.current,
      getRuntimeIdForStoredSession: () => activeSessionIdRef.current,
      requestGateway: routedRequest,
      resumeStoredSession: vi.fn(),
      updateSessionState: vi.fn((_sid, updater) => updater(createClientSessionState())),
      submitPromptText: vi.fn(async () => true),
      appendSessionTextMessage,
      branchCurrentSession: async () => true,
      handleSkinCommand: () => '',
      handoffSession: async () => ({ ok: true }),
      openMemoryGraph: vi.fn(),
      refreshSessions: async () => undefined,
      startFreshSessionDraft: vi.fn()
    })
  )

  return { ...hook, activeSessionIdRef, selectedStoredSessionIdRef, appendSessionTextMessage, createSession }
}

it.each(['ok', 'deferred'])('remembers a successful %s typed level for actual next-chat creation', async status => {
  const { result, appendSessionTextMessage } = setup(vi.fn(async () => ({ status, value: 'xhigh' })) as GatewayRequest)
  await act(async () => {
    await result.current('/reasoning xhigh')
  })

  if (status === 'deferred') {
    expect(appendSessionTextMessage).toHaveBeenCalledWith(
      'runtime-a',
      'system',
      expect.stringContaining('xhigh applies from your next message'),
      'stored-a'
    )
  }

  act(() => {
    applyRuntimeInfo({ reasoning_effort: 'xhigh' })
    restoreComposerReasoningEffort()
  })
  expect(getComposerReasoningEffort()).toBe('xhigh')
  expect((await desktopSessionCreateParams('', null)).reasoning_effort).toBe('xhigh')
})

it('remembers Off as none, not an unset default', async () => {
  const { result } = setup(vi.fn(async () => ({ value: 'none' })) as GatewayRequest)
  await act(async () => {
    await result.current('/reasoning none')
  })
  expect(getComposerReasoningEffort()).toBe('none')
  expect((await desktopSessionCreateParams('', null)).reasoning_effort).toBe('none')
})

it.each(['', 'show', 'hide', 'off', 'full', 'clamp'])(
  'does not remember read/display-only /reasoning %s',
  async word => {
    const { result } = setup(vi.fn(async () => ({ value: word || 'low' })) as GatewayRequest)
    await act(async () => {
      await result.current(`/reasoning ${word}`)
    })
    expect(getComposerReasoningEffort()).toBe('high')
    expect($currentReasoningEffort.get()).toBe('low')
  }
)

it('does not remember a refused level', async () => {
  const { result } = setup(
    vi.fn(async () => {
      throw new Error('refused')
    }) as GatewayRequest
  )

  await act(async () => {
    await result.current('/reasoning xhigh')
  })
  expect(getComposerReasoningEffort()).toBe('high')
})

it.each(['manual', 'session', 'profile', 'new-chat-profile'])(
  'fences a held slash reply after an A-B-A %s change',
  async kind => {
    const gate = deferred<{ value: string }>()
    const entered = deferred<void>()

    const { result, activeSessionIdRef } = setup(
      vi.fn(() => {
        entered.resolve()

        return gate.promise
      }) as GatewayRequest
    )

    let pending: Promise<boolean | void>
    act(() => {
      pending = result.current('/reasoning xhigh')
    })
    await entered.promise
    act(() => {
      if (kind === 'manual') {
        setCurrentReasoningEffort('medium')
        setCurrentReasoningEffort('xhigh')
      } else if (kind === 'session') {
        activeSessionIdRef.current = 'runtime-b'
        $activeSessionId.set('runtime-b')
        activeSessionIdRef.current = 'runtime-a'
        $activeSessionId.set('runtime-a')
      } else {
        const profile = kind === 'profile' ? $activeGatewayProfile : $newChatProfile
        profile.set('other')
        profile.set('default')
      }
    })
    await act(async () => {
      gate.resolve({ value: 'xhigh' })
      await pending!
    })
    expect(getComposerReasoningEffort()).toBe(kind === 'manual' ? 'xhigh' : 'high')
    expect($currentReasoningEffort.get()).toBe(kind === 'manual' ? 'xhigh' : 'low')
  }
)

it('does not repaint or remember a reply after the hook moves to another session', async () => {
  const gate = deferred<{ value: string }>()
  const entered = deferred<void>()

  const { result, activeSessionIdRef } = setup(
    vi.fn(() => {
      entered.resolve()

      return gate.promise
    }) as GatewayRequest
  )

  let pending: Promise<boolean | void>
  act(() => {
    pending = result.current('/reasoning xhigh')
  })
  await entered.promise
  activeSessionIdRef.current = 'runtime-b'
  $activeSessionId.set('runtime-b')
  setCurrentReasoningEffortTransient('minimal')
  await act(async () => {
    gate.resolve({ value: 'xhigh' })
    await pending!
  })
  expect(getComposerReasoningEffort()).toBe('high')
  expect($currentReasoningEffort.get()).toBe('minimal')
})

it('keeps a newer successful typed choice when an older reply arrives last', async () => {
  const first = deferred<{ value: string }>()
  const entered = deferred<void>()

  const request = vi
    .fn()
    .mockImplementationOnce(() => {
      entered.resolve()

      return first.promise
    })
    .mockResolvedValueOnce({ value: 'medium' })

  const { result } = setup(request as GatewayRequest)
  let pending: Promise<boolean | void>
  act(() => {
    pending = result.current('/reasoning xhigh')
  })
  await entered.promise
  await act(async () => {
    await result.current('/reasoning medium')
  })
  await act(async () => {
    first.resolve({ value: 'xhigh' })
    await pending!
  })
  expect(getComposerReasoningEffort()).toBe('medium')
})

it.each(['ok', 'deferred'])('remembers a fresh draft effort after its own %s create publication', async status => {
  const request = vi.fn(async () => ({ status, value: 'xhigh' })) as GatewayRequest
  const { result, createSession } = setup(request, {})
  await act(async () => {
    await result.current('/reasoning xhigh')
  })
  expect(createSession).toHaveBeenCalledOnce()
  expect(request).toHaveBeenCalledWith(
    'config.set',
    {
      key: 'reasoning',
      session_id: 'created-runtime',
      value: 'xhigh'
    },
    undefined
  )
  act(() => {
    restoreComposerReasoningEffort()
  })
  expect(getComposerReasoningEffort()).toBe('xhigh')
  expect((await desktopSessionCreateParams('', null)).reasoning_effort).toBe('xhigh')
})

function changeDraftContext(kind: string) {
  if (kind === 'manual') {
    setCurrentReasoningEffort('medium')
  } else if (kind === 'model') {
    const original = $currentModel.get()
    markComposerSelectionManual()
    setCurrentModel('other-model')
    markComposerSelectionManual()
    setCurrentModel(original)
  } else if (kind === 'session') {
    const original = $activeSessionId.get()
    $activeSessionId.set('other-runtime')
    $activeSessionId.set(original)
  } else if (kind === 'selection') {
    const original = $selectedStoredSessionId.get()
    $selectedStoredSessionId.set('other-stored')
    $selectedStoredSessionId.set(original)
  } else if (kind === 'authority') {
    $connection.set({ connectionId: 'other' } as NonNullable<ReturnType<typeof $connection.get>>)
    $connection.set(null)
  } else {
    const profile = kind === 'profile' ? $activeGatewayProfile : $newChatProfile
    profile.set('other')
    profile.set('default')
  }
}

it.each(['manual', 'model', 'session', 'selection', 'profile', 'new-chat-profile', 'authority'])(
  'does not forgive a pre-publication %s change on a fresh draft',
  async kind => {
    const { result } = setup(vi.fn(async () => ({ value: 'xhigh' })) as GatewayRequest, {
      beforePublish: () => changeDraftContext(kind)
    })

    await act(async () => {
      await result.current('/reasoning xhigh')
    })
    expect(getComposerReasoningEffort()).toBe(kind === 'manual' ? 'medium' : 'high')
  }
)

it.each(['manual', 'model', 'session', 'selection', 'profile', 'new-chat-profile', 'authority'])(
  'fences a fresh-draft reply after a post-publication %s change',
  async kind => {
    const reply = deferred<{ value: string }>()
    const entered = deferred<void>()

    const { result } = setup(
      vi.fn(() => {
        entered.resolve()

        return reply.promise
      }) as GatewayRequest,
      {}
    )

    let pending: Promise<boolean | void>
    act(() => {
      pending = result.current('/reasoning xhigh')
    })
    await entered.promise
    act(() => {
      changeDraftContext(kind)
    })
    await act(async () => {
      reply.resolve({ value: 'xhigh' })
      await pending!
    })
    expect(getComposerReasoningEffort()).toBe(kind === 'manual' ? 'medium' : 'high')
  }
)

it('does not issue an effort write or remember when fresh creation aborts', async () => {
  const request = vi.fn(async () => ({})) as GatewayRequest
  const { result, createSession } = setup(request, { aborted: true })
  await act(async () => {
    await result.current('/reasoning xhigh')
  })
  expect(createSession).toHaveBeenCalledOnce()
  expect(request).not.toHaveBeenCalled()
  expect(getComposerReasoningEffort()).toBe('high')
})

import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { resetBinding, setBinding } from '@/store/keybinds'
import { $activeGatewayProfile } from '@/store/profile'
import {
  $activeSessionId,
  $currentReasoningEffort,
  getComposerReasoningEffort,
  setCurrentReasoningEffort,
  setCurrentReasoningEffortTransient
} from '@/store/session'
import { deferred } from '@/test/deferred'

import { useKeybinds } from './use-keybinds'

vi.mock('@/themes/context', () => ({ useTheme: () => ({ resolvedMode: 'light', setMode: vi.fn() }) }))
afterEach(() => {
  cleanup()
  resetBinding('composer.reasoningUp')
})
beforeEach(() => {
  $activeSessionId.set('runtime-a')
  $activeGatewayProfile.set('default')
  setCurrentReasoningEffort('high')
  setCurrentReasoningEffortTransient('low')
  setBinding('composer.reasoningUp', ['alt+.'])
})

function setup() {
  const gate = deferred<{ value: string }>()
  const requestGateway = vi.fn(() => gate.promise)
  renderHook(
    () =>
      useKeybinds({
        archiveSelectedSession: vi.fn(),
        openNewSessionTab: vi.fn(),
        requestGateway: requestGateway as never,
        startFreshSession: vi.fn(),
        toggleCommandCenter: vi.fn(),
        toggleSelectedPin: vi.fn()
      }),
    { wrapper: ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter> }
  )
  act(() => {
    window.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: '.',
        code: 'Period',
        altKey: true,
        bubbles: true,
        cancelable: true
      })
    )
  })
  expect(requestGateway).toHaveBeenCalledWith('config.set', {
    key: 'reasoning',
    session_id: 'runtime-a',
    value: 'medium'
  })

  return gate
}

it.each(['high', ''])(
  'restores remembered %j independently from visible rollback after a real keybind refusal',
  async remembered => {
    setCurrentReasoningEffort(remembered)
    setCurrentReasoningEffortTransient('low')
    const gate = setup()
    await act(async () => {
      gate.reject(new Error('refused'))
    })
    await waitFor(() => expect($currentReasoningEffort.get()).toBe('low'))
    expect(getComposerReasoningEffort()).toBe(remembered)
    expect(localStorage.getItem('hermes.desktop.composer.reasoning-effort')).toBe(remembered || null)
  }
)

it.each(['reject', 'resolve'])('a held keybind %s cannot overwrite a newer A-B-A manual pick', async outcome => {
  const gate = setup()
  setCurrentReasoningEffort('xhigh')
  setCurrentReasoningEffort('medium')
  await act(async () => {
    if (outcome === 'reject') {gate.reject(new Error('refused'))}
    else {gate.resolve({ value: 'low' })}
  })
  expect(getComposerReasoningEffort()).toBe('medium')
  expect($currentReasoningEffort.get()).toBe('medium')
})

it.each(['session', 'profile'])('fences a held keybind refusal across an A-B-A %s switch', async kind => {
  const gate = setup()

  if (kind === 'session') {
    $activeSessionId.set('runtime-b')
    $activeSessionId.set('runtime-a')
  } else {
    $activeGatewayProfile.set('other')
    $activeGatewayProfile.set('default')
  }

  await act(async () => {
    gate.reject(new Error('refused'))
  })
  expect(getComposerReasoningEffort()).toBe('medium')
  expect($currentReasoningEffort.get()).toBe('medium')
})

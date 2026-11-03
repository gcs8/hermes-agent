import { beforeEach, expect, it, vi } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'
import { deferred } from '@/test/deferred'

import { applyModelPreset } from './model-presets'
import {
  $activeSessionId,
  $currentReasoningEffort,
  getComposerReasoningEffort,
  setComposerSelectionOwner,
  setCurrentReasoningEffort,
  setCurrentReasoningEffortTransient
} from './session'
import { $sessionStates, setSessionTileDelegate } from './session-states'

const key = 'hermes.desktop.composer.reasoning-effort'
const held = () => deferred<unknown>()

beforeEach(() => {
  $activeSessionId.set('effort-a')
  setComposerSelectionOwner('local', 'default')
  setCurrentReasoningEffort('high')
  setCurrentReasoningEffortTransient('low')
  $sessionStates.set({ 'effort-a': { ...createClientSessionState(), reasoningEffort: 'low' } })
})

const pick = (effort: string, request: ReturnType<typeof vi.fn>) =>
  applyModelPreset(
    { effort },
    {
      sessionId: 'effort-a',
      request: request as Parameters<typeof applyModelPreset>[1]['request'],
      failMessage: 'synthetic refusal'
    }
  )

it.each(['high', ''])('restores remembered %j independently of the viewed effort on refusal', async remembered => {
  setCurrentReasoningEffort(remembered)
  setCurrentReasoningEffortTransient('low')
  await pick(
    'xhigh',
    vi.fn(async () => {
      throw new Error('refused')
    })
  )
  expect($currentReasoningEffort.get()).toBe('low')
  expect(getComposerReasoningEffort()).toBe(remembered)
  expect(localStorage.getItem(key)).toBe(remembered || null)
})

it('does not roll back an A-B-A manual selection after a held failure', async () => {
  const gate = held()

  const pending = pick(
    'xhigh',
    vi.fn(() => gate.promise)
  )

  setCurrentReasoningEffort('medium')
  setCurrentReasoningEffort('xhigh')
  gate.reject(new Error('refused'))
  await pending
  expect($currentReasoningEffort.get()).toBe('xhigh')
  expect(getComposerReasoningEffort()).toBe('xhigh')
})

it.each(['session', 'owner'])('does not roll back after an A-B-A %s switch', async kind => {
  const gate = held()

  const pending = pick(
    'xhigh',
    vi.fn(() => gate.promise)
  )

  if (kind === 'session') {
    $activeSessionId.set('effort-b')
    $activeSessionId.set('effort-a')
  } else {
    setComposerSelectionOwner('local', 'other')
    setComposerSelectionOwner('local', 'default')
  }

  gate.reject(new Error('refused'))
  await pending
  expect($currentReasoningEffort.get()).toBe('xhigh')
  expect(getComposerReasoningEffort()).toBe('xhigh')
})

it('rolls back to a successful preceding queued pick, not its viewed or optimistic predecessor', async () => {
  const first = held()
  const second = held()
  const entered = deferred<void>()

  const request = vi
    .fn()
    .mockImplementationOnce(() => first.promise)
    .mockImplementationOnce(() => {
      entered.resolve()

      return second.promise
    })

  const p1 = pick('medium', request)
  const p2 = pick('xhigh', request)
  first.resolve({ value: 'medium' })
  await entered.promise
  second.reject(new Error('refused'))
  await Promise.all([p1, p2])
  expect($currentReasoningEffort.get()).toBe('medium')
  expect(getComposerReasoningEffort()).toBe('medium')
})

it('does not let the first failed A-B-A queued pick repaint the latest optimistic slice', async () => {
  const gates = [held(), held(), held()]
  const entered = [deferred<void>(), deferred<void>(), deferred<void>()]
  const request = vi.fn()
  gates.forEach((gate, index) =>
    request.mockImplementationOnce(() => {
      entered[index]!.resolve()

      return gate.promise
    })
  )
  setSessionTileDelegate({
    archiveSession: vi.fn(),
    branchSession: vi.fn(),
    branchSessionAtMessage: vi.fn(),
    deleteSession: vi.fn(),
    executeSlash: vi.fn(),
    interruptSession: vi.fn(),
    resumeTile: vi.fn(),
    submitToSession: vi.fn(),
    updateSession: (id, update) => {
      const state = update($sessionStates.get()[id]!)
      $sessionStates.set({ ...$sessionStates.get(), [id]: state })

      return state
    }
  })
  const p1 = pick('xhigh', request)
  const p2 = pick('medium', request)
  const p3 = pick('xhigh', request)
  await entered[0]!.promise
  gates[0]!.reject(new Error('refused'))
  await entered[1]!.promise
  expect($sessionStates.get()['effort-a']!.reasoningEffort).toBe('xhigh')
  expect(getComposerReasoningEffort()).toBe('xhigh')
  gates[1]!.resolve({})
  await entered[2]!.promise
  gates[2]!.resolve({})
  await Promise.all([p1, p2, p3])
  expect(getComposerReasoningEffort()).toBe('xhigh')
})

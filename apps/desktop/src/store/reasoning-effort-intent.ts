import { $activeGatewayProfile, $newChatProfile } from './profile'
import {
  $activeSessionId,
  $connection,
  $selectedStoredSessionId,
  getComposerReasoningEffortGeneration,
  getComposerSelectionGeneration
} from './session'

// Keep only the last transition, not a growing history. A capture can admit
// exactly one null -> created-id publication, never a navigation round trip.
function sessionPublication(session: typeof $activeSessionId) {
  let revision = Symbol()
  let previousRevision = revision
  let from = session.get()

  session.listen((_value, previous) => {
    previousRevision = revision
    revision = Symbol()
    from = previous
  })

  return () => {
    const captured = revision
    const startedEmpty = session.get() === null

    return (createdId?: string | null) => {
      if (createdId === undefined) {
        return captured === revision
      }

      return (
        session.get() === createdId &&
        (captured === revision || (startedEmpty && from === null && previousRevision === captured))
      )
    }
  }
}

const captureActivePublication = sessionPublication($activeSessionId)
const captureSelectedPublication = sessionPublication($selectedStoredSessionId)

// Profile/connection changes are never rebased by a command's own creation.
// Runtime effort snapshots remain observations, not explicit intent.
let ownerGeneration = 0

const invalidate = () => {
  ownerGeneration += 1
}

$activeGatewayProfile.listen(invalidate)
$newChatProfile.listen(invalidate)

const connectionAuthority = () => {
  const connection = $connection.get()

  return JSON.stringify(
    connection && [
      connection.baseUrl,
      connection.wsUrl,
      connection.token,
      connection.connectionId,
      connection.mode,
      connection.profile,
      connection.authMode
    ]
  )
}

let authority = connectionAuthority()
$connection.listen(() => {
  const next = connectionAuthority()

  if (next !== authority) {
    authority = next
    invalidate()
  }
})

/** Fence renderer settlement, not the already captured session's gateway write. */
export function captureReasoningEffortIntent() {
  const owner = ownerGeneration
  const effort = getComposerReasoningEffortGeneration()
  const model = getComposerSelectionGeneration()
  const activeIsCurrent = captureActivePublication()
  const selectedIsCurrent = captureSelectedPublication()

  return (created?: { runtimeId: string; storedId: string | null }) =>
    owner === ownerGeneration &&
    effort === getComposerReasoningEffortGeneration() &&
    model === getComposerSelectionGeneration() &&
    activeIsCurrent(created?.runtimeId) &&
    selectedIsCurrent(created?.storedId)
}

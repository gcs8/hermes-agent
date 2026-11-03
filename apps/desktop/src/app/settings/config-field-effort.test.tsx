import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeAll, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

import { ConfigField } from './config-field'
import { ENUM_OPTIONS } from './constants'

beforeAll(() => {
  Element.prototype.scrollIntoView ??= vi.fn()
})

afterEach(cleanup)

it('labels blank worker effort Inherit and preserves blank, explicit level and disabled payloads', () => {
  const onChange = vi.fn()

  function WorkerEffort() {
    const [value, setValue] = useState('')

    return (
      <ConfigField
        enumOptions={[...ENUM_OPTIONS['delegation.reasoning_effort'], 'disabled']}
        onChange={next => {
          onChange(next)
          setValue(String(next))
        }}
        schema={{ type: 'select' }}
        schemaKey="delegation.reasoning_effort"
        value={value}
      />
    )
  }

  render(
    <I18nProvider configClient={null} initialLocale="en">
      <WorkerEffort />
    </I18nProvider>
  )

  expect(screen.getByRole('combobox').textContent).toBe('Inherit')
  expect(onChange).not.toHaveBeenCalled()

  fireEvent.keyDown(screen.getByRole('combobox'), { key: 'ArrowDown' })
  fireEvent.click(screen.getByRole('option', { name: 'High' }))
  expect(onChange).toHaveBeenLastCalledWith('high')
  expect(screen.getByRole('combobox').textContent).toBe('High')

  fireEvent.keyDown(screen.getByRole('combobox'), { key: 'ArrowDown' })
  fireEvent.click(screen.getByRole('option', { name: 'Inherit' }))
  expect(onChange).toHaveBeenLastCalledWith('')
  expect(screen.getByRole('combobox').textContent).toBe('Inherit')

  // `disabled` is a supported reasoning literal, not the blank inheritance sentinel.
  fireEvent.keyDown(screen.getByRole('combobox'), { key: 'ArrowDown' })
  fireEvent.click(screen.getByRole('option', { name: 'Disabled' }))
  expect(onChange).toHaveBeenLastCalledWith('disabled')
  expect(screen.getByRole('combobox').textContent).toBe('Disabled')
  expect(onChange.mock.calls).toEqual([['high'], [''], ['disabled']])
})

it('keeps unrelated blank choices and caller-supplied option labels unchanged', () => {
  const onChange = vi.fn()

  const { rerender } = render(
    <I18nProvider configClient={null} initialLocale="en">
      <ConfigField
        onChange={onChange}
        optionLabels={{ fast: 'Faster responses' }}
        schema={{ type: 'select', options: ['', 'fast'] }}
        schemaKey="agent.service_tier"
        value=""
      />
    </I18nProvider>
  )

  expect(screen.getByRole('combobox').textContent).toBe('(none)')
  fireEvent.keyDown(screen.getByRole('combobox'), { key: 'ArrowDown' })
  fireEvent.click(screen.getByRole('option', { name: 'Faster responses' }))
  expect(onChange).toHaveBeenCalledExactlyOnceWith('fast')

  rerender(
    <I18nProvider configClient={null} initialLocale="en">
      <ConfigField
        enumOptions={ENUM_OPTIONS['delegation.reasoning_effort']}
        onChange={onChange}
        optionLabels={{ high: 'Custom high effort' }}
        schema={{ type: 'select' }}
        schemaKey="delegation.reasoning_effort"
        value="high"
      />
    </I18nProvider>
  )
  expect(screen.getByRole('combobox').textContent).toBe('Custom high effort')
})

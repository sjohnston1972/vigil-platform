import { render, screen } from '@testing-library/react'
import { BudgetsPage } from '../../components/admin/BudgetsPage'

describe('BudgetsPage', () => {
  it('renders all five column headers: Tenant, Daily Limit, Used Today, Reset, Edit', () => {
    render(<BudgetsPage />)
    const headers = screen.getAllByRole('columnheader')
    const headerTexts = headers.map(h => h.textContent)
    expect(headerTexts).toContain('Tenant')
    expect(headerTexts).toContain('Daily Limit')
    expect(headerTexts).toContain('Used Today')
    expect(headerTexts).toContain('Reset')
    expect(headerTexts).toContain('Edit')
  })

  it('displays usage as "42k / 100k" format for Acme Corp (42180/100000)', () => {
    render(<BudgetsPage />)
    // The cell renders as separate text nodes: "42k", " / ", "100k"
    // Match using a function that checks the full text content of the cell
    const acmeCells = screen.getAllByText((_, el) =>
      el?.tagName === 'TD' && el.textContent === '42k / 100k'
    )
    expect(acmeCells.length).toBeGreaterThan(0)
  })

  it('a row with > 80% usage has text-amber-500 class', () => {
    render(<BudgetsPage />)
    // Delta LLC: 85000/100000 = 85% — cell gets text-amber-500
    const amberCells = screen.getAllByText((_, el) =>
      el?.tagName === 'TD' && el.textContent === '85k / 100k'
    )
    expect(amberCells.length).toBeGreaterThan(0)
    expect(amberCells[0]).toHaveClass('text-amber-500')
  })

  it('a row with > 95% usage has text-red-500 class', () => {
    render(<BudgetsPage />)
    // Epsilon GmbH: 96000/100000 = 96% — cell gets text-red-500
    const redCells = screen.getAllByText((_, el) =>
      el?.tagName === 'TD' && el.textContent === '96k / 100k'
    )
    expect(redCells.length).toBeGreaterThan(0)
    expect(redCells[0]).toHaveClass('text-red-500')
  })

  it('Edit renders as an <a> element', () => {
    render(<BudgetsPage />)
    const editLinks = screen.getAllByRole('link', { name: /edit/i })
    expect(editLinks.length).toBeGreaterThan(0)
    editLinks.forEach(link => {
      expect(link.tagName).toBe('A')
    })
  })
})

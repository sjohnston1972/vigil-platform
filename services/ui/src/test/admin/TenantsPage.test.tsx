import { render, screen, within } from '@testing-library/react'
import { TenantsPage } from '../../components/admin/TenantsPage'

describe('TenantsPage', () => {
  it('renders all four column headers: Tenant, Status, Budget Used, Edit', () => {
    render(<TenantsPage />)
    const headers = screen.getAllByRole('columnheader')
    const headerTexts = headers.map(h => h.textContent)
    expect(headerTexts).toContain('Tenant')
    expect(headerTexts).toContain('Status')
    expect(headerTexts).toContain('Budget Used')
    expect(headerTexts).toContain('Edit')
  })

  it('Acme Corp row has green dot and "Active" text', () => {
    render(<TenantsPage />)
    const acmeRow = screen.getByText('Acme Corp').closest('tr')!
    const acmeStatus = within(acmeRow).getByText(/Active/)
    expect(acmeStatus).toBeInTheDocument()
    expect(acmeStatus).toHaveClass('text-green-500')
  })

  it('Gamma Ltd row has red dot and "Suspended" text', () => {
    render(<TenantsPage />)
    const gammaRow = screen.getByText('Gamma Ltd').closest('tr')!
    const gammaStatus = within(gammaRow).getByText(/Suspended/)
    expect(gammaStatus).toBeInTheDocument()
    expect(gammaStatus).toHaveClass('text-red-500')
  })

  it('Edit renders as an <a> element', () => {
    render(<TenantsPage />)
    const editLinks = screen.getAllByRole('link', { name: /edit/i })
    expect(editLinks.length).toBeGreaterThan(0)
    editLinks.forEach(link => {
      expect(link.tagName).toBe('A')
    })
  })
})

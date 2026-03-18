import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { Sidebar } from '../../components/shell/Sidebar'

describe('Sidebar', () => {
  it('renders VIGIL wordmark', () => {
    render(<MemoryRouter><Sidebar tenantName="Acme Corp" tokensToday={42180} /></MemoryRouter>)
    expect(screen.getByText('VIGIL')).toBeInTheDocument()
  })

  it('renders all top-level nav items', () => {
    render(<MemoryRouter><Sidebar tenantName="Acme Corp" tokensToday={42180} /></MemoryRouter>)
    expect(screen.getByText('Dashboard')).toBeInTheDocument()
    expect(screen.getByText('Chat')).toBeInTheDocument()
    expect(screen.getByText('Audit Logs')).toBeInTheDocument()
    expect(screen.getByText('Admin')).toBeInTheDocument()
  })

  it('shows tenant name at the bottom', () => {
    render(<MemoryRouter><Sidebar tenantName="Acme Corp" tokensToday={42180} /></MemoryRouter>)
    expect(screen.getByText('Acme Corp')).toBeInTheDocument()
  })

  it('shows today token count', () => {
    render(<MemoryRouter><Sidebar tenantName="Acme Corp" tokensToday={42180} /></MemoryRouter>)
    expect(screen.getByText(/42,180/)).toBeInTheDocument()
  })

  it('shows admin sub-nav when on admin route', () => {
    render(
      <MemoryRouter initialEntries={['/admin/tenants']}>
        <Sidebar tenantName="Acme Corp" tokensToday={0} />
      </MemoryRouter>
    )
    expect(screen.getByText('Tenants')).toBeInTheDocument()
    expect(screen.getByText('Token Budgets')).toBeInTheDocument()
    expect(screen.getByText('Agent Health')).toBeInTheDocument()
  })

  it('hides admin sub-nav on non-admin routes', () => {
    render(
      <MemoryRouter initialEntries={['/dashboard']}>
        <Sidebar tenantName="Acme Corp" tokensToday={0} />
      </MemoryRouter>
    )
    expect(screen.queryByText('Tenants')).not.toBeInTheDocument()
  })
})

import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { NavItem } from '../../components/shell/NavItem'

function wrap(path: string, element: React.ReactNode) {
  return (
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="*" element={<>{element}</>} />
      </Routes>
    </MemoryRouter>
  )
}

describe('NavItem', () => {
  it('renders label', () => {
    render(wrap('/dashboard', <NavItem to="/dashboard" label="Dashboard" icon="⊞" />))
    expect(screen.getByText('Dashboard')).toBeInTheDocument()
  })

  it('has active class when route matches', () => {
    render(wrap('/dashboard', <NavItem to="/dashboard" label="Dashboard" icon="⊞" />))
    const item = screen.getByRole('link')
    expect(item).toHaveClass('border-vigil-accent')
  })

  it('does not have active class on non-matching route', () => {
    render(wrap('/audit', <NavItem to="/dashboard" label="Dashboard" icon="⊞" />))
    const item = screen.getByRole('link')
    expect(item).not.toHaveClass('border-vigil-accent')
  })
})

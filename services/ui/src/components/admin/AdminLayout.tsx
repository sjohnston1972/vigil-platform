import { Outlet } from 'react-router-dom'

export function AdminLayout() {
  return (
    <div className="p-4 text-vigil-body font-mono">
      <Outlet />
    </div>
  )
}

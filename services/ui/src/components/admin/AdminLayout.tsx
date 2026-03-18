import { Outlet } from 'react-router-dom'

export function AdminLayout() {
  return (
    <div className="h-full overflow-y-auto p-6">
      <Outlet />
    </div>
  )
}

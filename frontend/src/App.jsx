import { useEffect, useState } from "react";
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { currentUser } from "./auth.js";
import { TopBar, UserContext } from "./components/common.jsx";
import Access from "./pages/Access.jsx";
import AddData from "./pages/AddData.jsx";
import Chat from "./pages/Chat.jsx";
import Login from "./pages/Login.jsx";
import Processing from "./pages/Processing.jsx";
import Review from "./pages/Review.jsx";
import Workspace from "./pages/Workspace.jsx";

// The server decides whether the session is valid; the UI only asks who is signed in.
function Protected() {
  const location = useLocation();
  const [user, setUser] = useState(undefined);
  useEffect(() => {
    currentUser().then(setUser).catch(() => setUser(null));
  }, []);
  if (user === null) {
    return <Navigate to={`/login?next=${encodeURIComponent(location.pathname + location.search)}`} replace />;
  }
  if (!user) return <div className="page muted">Loading…</div>;
  return (
    <UserContext.Provider value={user}>
      <div className="app">
        <TopBar />
        <Outlet />
      </div>
    </UserContext.Provider>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route element={<Protected />}>
          <Route path="/workspace" element={<Workspace />} />
          <Route path="/kbs/:kb/jobs/:jobId" element={<Processing />} />
          <Route path="/kbs/:kb/review" element={<Review />} />
          <Route path="/access" element={<Access />} />
          <Route path="/add-data" element={<AddData />} />
          <Route path="/chat" element={<Chat />} />
        </Route>
        <Route path="*" element={<Navigate to="/workspace" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

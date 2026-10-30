import { Routes, Route, Navigate } from "react-router-dom";
import AuthLayout from "@/components/AuthLayout";
import AdminLayout from "@/components/AdminLayout";
import LoginPage from "@/pages/LoginPage/LoginPage";
import AdminDashboard from "@/pages/Admin/AdminDashboard";
import AdminKnowledge from "@/pages/Admin/AdminKnowledge";
import AdminKbAgent from "@/pages/Admin/AdminKbAgent";
import AdminQuestions from "@/pages/Admin/AdminQuestions";
import AdminAnalytics from "@/pages/Admin/AdminAnalytics";
import AdminCards from "@/pages/Admin/AdminCards";
import AdminOrders from "@/pages/Admin/AdminOrders";
import AdminLostFound from "@/pages/Admin/AdminLostFound";
import AdminContent from "@/pages/Admin/AdminContent";
import AdminConfig from "@/pages/Admin/AdminConfig";
import AdminUsers from "@/pages/Admin/AdminUsers";
import AdminProfile from "@/pages/Admin/AdminProfile";
import NotFoundPage from "@/pages/NotFoundPage/NotFoundPage";

export default function App() {
  return (
    <Routes>
      {/* 认证守卫外层（登录页公开；管理区要求 admin 角色，由 AdminLayout 二次守卫） */}
      <Route element={<AuthLayout />}>
        <Route path="login" element={<LoginPage />} />

        {/* 管理端 PC（AI 问答已迁移至微信小程序，此处为纯运营后台） */}
        <Route path="admin" element={<AdminLayout />}>
          <Route index element={<AdminDashboard />} />
          <Route path="knowledge" element={<AdminKnowledge />} />
          <Route path="kb-agent" element={<AdminKbAgent />} />
          <Route path="questions" element={<AdminQuestions />} />
          <Route path="analytics" element={<AdminAnalytics />} />
          <Route path="cards" element={<AdminCards />} />
          <Route path="orders" element={<AdminOrders />} />
          <Route path="lost-found" element={<AdminLostFound />} />
          <Route path="content" element={<AdminContent />} />
          <Route path="config" element={<AdminConfig />} />
          <Route path="users" element={<AdminUsers />} />
          {/* 个人信息（侧栏底部头像入口） */}
          <Route path="profile" element={<AdminProfile />} />
        </Route>

        {/* 根路径与游客路由统一重定向到管理后台 */}
        <Route index element={<Navigate to="/admin" replace />} />
        <Route path="profile" element={<Navigate to="/admin" replace />} />
        <Route path="register" element={<Navigate to="/login" replace />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}

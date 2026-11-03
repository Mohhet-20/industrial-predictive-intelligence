import React from "react";
import { Navigate, Route, Routes } from "react-router-dom";

import { useAuth } from "@/auth/useAuth";
import { Layout } from "@/components/Layout";
import { LoadingState } from "@/components/StateViews";
import { AssetDetailPage } from "@/pages/AssetDetailPage";
import { AssetsPage } from "@/pages/AssetsPage";
import { ConditionPage } from "@/pages/ConditionPage";
import { DataExplorerPage } from "@/pages/DataExplorerPage";
import { DataQualityPage } from "@/pages/DataQualityPage";
import { IntelligencePage } from "@/pages/IntelligencePage";
import { IntelligenceWorkspacePage } from "@/pages/IntelligenceWorkspacePage";
import { LoginPage } from "@/pages/LoginPage";
import { MlopsPage } from "@/pages/MlopsPage";
import { OutcomeLearningCenterPage } from "@/pages/OutcomeLearningCenterPage";
import { OverviewPage } from "@/pages/OverviewPage";
import { ScenarioLabPage } from "@/pages/ScenarioLabPage";
import { SystemHealthPage } from "@/pages/SystemHealthPage";
import { TemporalAnalysisPage } from "@/pages/TemporalAnalysisPage";

function RequireAuth({ children }: { children: React.ReactNode }) {
  const { token, isLoading } = useAuth();
  if (isLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <LoadingState label="Checking session..." />
      </div>
    );
  }
  if (!token) {
    return <Navigate to="/login" replace />;
  }
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route path="/" element={<OverviewPage />} />
        <Route path="/workspace" element={<IntelligenceWorkspacePage />} />
        <Route path="/assets" element={<AssetsPage />} />
        <Route path="/assets/:assetId" element={<AssetDetailPage />} />
        <Route path="/explorer" element={<DataExplorerPage />} />
        <Route path="/data-quality" element={<DataQualityPage />} />
        <Route path="/temporal-analysis" element={<TemporalAnalysisPage />} />
        <Route path="/intelligence" element={<IntelligencePage />} />
        <Route path="/condition" element={<ConditionPage />} />
        <Route path="/scenarios" element={<ScenarioLabPage />} />
        <Route path="/outcomes" element={<OutcomeLearningCenterPage />} />
        <Route path="/mlops" element={<MlopsPage />} />
        <Route path="/system" element={<SystemHealthPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

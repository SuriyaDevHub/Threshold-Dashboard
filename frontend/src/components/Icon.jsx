import {
  LayoutDashboard,
  SlidersHorizontal,
  ListChecks,
  Boxes,
  AlertTriangle,
  Activity,
  GitBranch,
} from "lucide-react";

// Map the icon strings declared in module meta -> lucide components.
const MAP = {
  dashboard: LayoutDashboard,
  sliders: SlidersHorizontal,
  "list-checks": ListChecks,
  boxes: Boxes,
  alert: AlertTriangle,
  activity: Activity,
  "git-branch": GitBranch,
};

export default function Icon({ name, size = 18, ...rest }) {
  const Cmp = MAP[name] || Boxes;
  return <Cmp size={size} {...rest} />;
}

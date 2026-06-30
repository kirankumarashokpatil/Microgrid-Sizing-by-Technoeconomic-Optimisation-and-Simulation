// One place that wires react-plotly.js to the lightweight dist-min Plotly build,
// so we don't need extra build config. Every chart imports Plot from here.
import createPlotlyComponent from "react-plotly.js/factory";
import Plotly from "plotly.js-dist-min";

const Plot = createPlotlyComponent(Plotly);
export default Plot;

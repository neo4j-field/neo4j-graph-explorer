// Renders a PyVis HTML file inline as an iframe inside the Chainlit message.
// `props` is injected by the Chainlit runtime and contains the keys passed
// from cl.CustomElement(props={...}) on the Python side.
export default function GraphViz() {
  return (
    <div style={{
      width: "100%",
      borderRadius: "8px",
      overflow: "hidden",
      backgroundColor: "#1f1f23",
      border: "1px solid #2a2a30"
    }}>
      <iframe
        src={props.url}
        title="Neo4j Graph"
        style={{
          width: "100%",
          height: `${props.height || 640}px`,
          border: "none",
          display: "block"
        }}
        sandbox="allow-scripts allow-same-origin"
      />
    </div>
  );
}
